"""Pass causality: which pass invocation made a later one do its work.

A pass's dump says what it changed, not why it could. The why is counterfactual:
run the pipeline again with that one invocation skipped, and see which later
invocations stop changing the IR (it *enabled* them) and which start (it
*pre-empted* them — they would have done the work had it not got there first).
`plugins/ProvenanceTracker.cpp` does both halves inside opt: it logs every
invocation and what it changed, and `-prov-skip=<key>` skips exactly one.

Observed tracker output (stderr, tab-separated), LLVM 22:

    PROV-RUN   63  scale_add/FunctionToLoopPassAdaptor#1/LoopRotatePass
               LoopRotatePass  loop-rotate  loop %<unnamed> in @scale_add
    PROV       63  scale_add/FunctionToLoopPassAdaptor#1/LoopRotatePass
               scale_add  changed  3b1ec11e828e8ae6
    PROV-SKIP  scale_add/FunctionToLoopPassAdaptor#1/LoopRotatePass
               LoopRotatePass  loop %<unnamed> in @scale_add  ablated

The ordinal numbers invocations the way -debug-pass-manager numbers its
"Running pass:" lines, so a baseline record lands on a lane-A run by position;
across runs, where skipping one invocation shifts every ordinal after it, the
key names the invocation instead.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .proc import ProcError, run_capture
from .toolchain import Toolchain, discover_toolchain

TRACKER_NAME = "ProvenanceTracker.cpp"
CACHE_ENV = "XDG_CACHE_HOME"
DEFAULT_LIMIT = 400  # ablation runs; each is one full opt run with no dumps

OUTPUT_WRITERS = frozenset({"PrintModulePass", "BitcodeWriterPass"})

ENABLES = "enables"
PREEMPTS = "preempts"


class CausalityError(RuntimeError):
    """The tracker could not be built, or the baseline run did not complete."""


# --- the tracker plugin -------------------------------------------------------


def tracker_source() -> Path:
    return Path(files("llvm_lens") / "plugins" / TRACKER_NAME)


def _sibling(tool: Path, name: str, major: int) -> str | None:
    """*name* next to *tool*, in the same spelling (clang-22 -> clang++-22)."""
    suffix = tool.name[len("clang"):] if tool.name.startswith("clang") else ""
    for candidate in (tool.with_name(name + suffix), tool.with_name(name)):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(f"{name}-{major}") or shutil.which(name)


def _cache_dir() -> Path:
    base = os.environ.get(CACHE_ENV)
    root = Path(base).expanduser() if base else Path("~/.cache").expanduser()
    return root / "llvm-lens"


def build_tracker(toolchain: Toolchain, cache_dir: Path | None = None) -> Path:
    """Compile the tracker against the toolchain's LLVM, once per source+LLVM."""
    major = toolchain.expected_major
    cxx = _sibling(toolchain.clang.path, "clang++", major)
    config = _sibling(toolchain.clang.path, "llvm-config", major)
    if cxx is None or config is None:
        raise CausalityError(
            f"building the provenance tracker needs clang++ and llvm-config for "
            f"LLVM {major} (found clang++={cxx}, llvm-config={config}); "
            "or pass a prebuilt one with --causality-plugin"
        )
    try:
        version = subprocess.run([config, "--version"], capture_output=True,
                                 text=True, check=True).stdout.strip()
        cxxflags = subprocess.run([config, "--cxxflags"], capture_output=True,
                                  text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CausalityError(f"cannot run {config}: {exc}") from exc
    if not version.startswith(f"{major}."):
        raise CausalityError(f"{config} is LLVM {version}, expected {major}")

    source = tracker_source()
    digest = hashlib.sha256(
        source.read_bytes() + "\0".join([cxx, version, *cxxflags]).encode()
    ).hexdigest()[:16]
    cache = cache_dir or _cache_dir()
    target = cache / f"libProvTracker-{version}-{digest}.so"
    if target.is_file():
        return target

    cache.mkdir(parents=True, exist_ok=True)
    # Compiled beside the target and renamed into place, so two reports
    # building at once never load a half-written .so.
    fd, partial = tempfile.mkstemp(suffix=".so", dir=cache)
    os.close(fd)
    cmd = [cxx, "-shared", "-fPIC", "-O2", *cxxflags, str(source), "-o", partial]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        Path(partial).unlink(missing_ok=True)
        tail = "\n".join(proc.stderr.splitlines()[-15:])
        raise CausalityError(f"building the provenance tracker failed:\n{tail}")
    os.replace(partial, target)
    return target


# --- one traced run -----------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    """One pass invocation, as the tracker's PROV-RUN line names it."""

    ord: int
    key: str
    cls: str  # pass class name, as -debug-pass-manager prints it
    name: str  # pipeline name (loop-rotate), or the class when it has none
    unit: str  # the IR unit it ran on, for display


@dataclass(frozen=True)
class Change:
    ord: int
    key: str
    entity: str  # a function name, or [module] for globals and declarations
    event: str  # changed | created | deleted
    digest: str


@dataclass
class Trace:
    runs: list[Invocation] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (key, why)
    cmd: tuple[str, ...] = ()
    returncode: int = 0
    timed_out: bool = False
    final_digest: str | None = None
    stderr_tail: str = ""

    @property
    def failed(self) -> bool:
        return self.timed_out or self.returncode != 0

    def effects(self) -> dict[str, set[str]]:
        """key -> the entities that invocation changed."""
        out: dict[str, set[str]] = {}
        for change in self.changes:
            out.setdefault(change.key, set()).add(change.entity)
        return out


def parse_trace(stderr: str) -> Trace:
    trace = Trace()
    for line in stderr.splitlines():
        if line.startswith("PROV-RUN\t"):
            parts = line.split("\t")
            if len(parts) >= 6:
                trace.runs.append(Invocation(int(parts[1]), parts[2], parts[3],
                                             parts[4], parts[5]))
        elif line.startswith("PROV\t"):
            parts = line.split("\t")
            if len(parts) >= 6:
                trace.changes.append(Change(int(parts[1]), parts[2], parts[3],
                                            parts[4], parts[5]))
        elif line.startswith("PROV-SKIP\t"):
            parts = line.split("\t")
            if len(parts) >= 5:
                trace.skipped.append((parts[1], parts[4]))
    return trace


def trace_command(
    toolchain: Toolchain,
    tracker: Path,
    input_ir: Path,
    passes: str,
    output: Path | str,
    load_pass_plugins: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    skip: Sequence[str] = (),
) -> list[str]:
    # The tracker loads first: its -prov-skip option only exists once it has.
    cmd = [str(toolchain.opt.path), "-S", f"-load-pass-plugin={tracker}"]
    cmd += [f"-load-pass-plugin={plugin}" for plugin in load_pass_plugins]
    cmd += [f"-passes={passes}", *extra_args]
    cmd += [f"-prov-skip={key}" for key in skip]
    cmd += ["-o", str(output), str(input_ir)]
    return cmd


def run_trace(
    toolchain: Toolchain,
    tracker: Path,
    input_ir: Path,
    passes: str,
    load_pass_plugins: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    skip: Sequence[str] = (),
    timeout: float | None = None,
) -> Trace:
    """Run opt once under the tracker; the final IR is kept only as a digest."""
    with tempfile.TemporaryDirectory(prefix="llvm-lens-prov-") as scratch:
        out = Path(scratch) / "out.ll"
        cmd = trace_command(toolchain, tracker, input_ir, passes, out,
                            load_pass_plugins, extra_args, skip)
        try:
            result = run_capture(cmd, timeout)
        except ProcError as exc:
            raise CausalityError(str(exc)) from exc
        trace = parse_trace(result.stderr)
        trace.cmd = tuple(cmd)
        trace.returncode = result.returncode
        trace.timed_out = result.timed_out
        trace.stderr_tail = "\n".join(
            line for line in result.stderr.splitlines()[-12:]
            if not line.startswith("PROV"))
        if out.is_file() and not trace.failed:
            trace.final_digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return trace


# --- from traces to a graph ---------------------------------------------------


@dataclass
class Node:
    key: str
    cls: str
    name: str
    unit: str
    ords: list[int] = field(default_factory=list)  # every baseline run of it
    changed_ords: list[int] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)  # what it changed
    # Skipping it: does the final IR change? None when it was not ablated.
    final_differs: bool | None = None
    error: str | None = None
    in_baseline: bool = True


@dataclass(frozen=True)
class Edge:
    cause: str
    effect: str
    kind: str  # ENABLES | PREEMPTS
    entities: tuple[str, ...]
    direct: bool = True


@dataclass
class CausalGraph:
    nodes: dict[str, Node]
    edges: list[Edge]
    ablated: int
    candidates: int
    baseline_cmd: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return self.ablated < self.candidates


def _nodes(baseline: Trace) -> dict[str, Node]:
    nodes: dict[str, Node] = {}
    for run in baseline.runs:
        node = nodes.get(run.key)
        if node is None:
            node = nodes[run.key] = Node(run.key, run.cls, run.name, run.unit)
        node.ords.append(run.ord)
    for change in baseline.changes:
        node = nodes.get(change.key)
        if node is None:
            continue
        if change.ord not in node.changed_ords:
            node.changed_ords.append(change.ord)
        if change.entity not in node.entities:
            node.entities.append(change.entity)
    return nodes


def candidates_of(baseline: Trace) -> list[str]:
    """Every invocation that changed something, in pipeline order."""
    seen: dict[str, None] = {}
    for change in sorted(baseline.changes, key=lambda c: c.ord):
        seen.setdefault(change.key, None)
    return list(seen)


def _mark_direct(edges: list[Edge], nodes: dict[str, Node]) -> list[Edge]:
    """An enabling edge is direct unless a longer enabling path explains it.

    Pipeline order is a topological order (an invocation can only enable a
    later one), so reachability is one sweep from the back.
    """
    out_of: dict[str, set[str]] = {}
    for edge in edges:
        if edge.kind == ENABLES:
            out_of.setdefault(edge.cause, set()).add(edge.effect)
    first = {key: min(node.ords or [1 << 30]) for key, node in nodes.items()}
    reach: dict[str, set[str]] = {}
    for key in sorted(out_of, key=lambda k: -first.get(k, 0)):
        seen: set[str] = set()
        for nxt in out_of[key]:
            seen.add(nxt)
            seen |= reach.get(nxt, set())
        reach[key] = seen
    marked = []
    for edge in edges:
        direct = True
        if edge.kind == ENABLES:
            direct = not any(edge.effect in reach.get(mid, ())
                             for mid in out_of.get(edge.cause, ()) if mid != edge.effect)
        marked.append(Edge(edge.cause, edge.effect, edge.kind, edge.entities, direct))
    return marked


def build_graph(baseline: Trace, ablations: dict[str, Trace],
                candidates: int | None = None) -> CausalGraph:
    """Compare each ablated run against the baseline, invocation by invocation."""
    nodes = _nodes(baseline)
    base = baseline.effects()
    edges: list[Edge] = []
    for cause, trace in ablations.items():
        node = nodes.get(cause)
        if node is None:
            continue
        if trace.failed or not any(k == cause for k, _ in trace.skipped):
            node.error = ("opt failed with this invocation skipped:\n" + trace.stderr_tail
                          if trace.failed else "the skip did not take effect")
            continue
        node.final_differs = trace.final_digest != baseline.final_digest
        start = min(node.changed_ords or node.ords)
        now = trace.effects()
        for key, entities in base.items():
            if key == cause or min(nodes[key].changed_ords or [0]) <= start:
                continue
            lost = entities - now.get(key, set())
            if lost:
                edges.append(Edge(cause, key, ENABLES, tuple(sorted(lost))))
        for key, entities in now.items():
            if key == cause:
                continue
            gained = entities - base.get(key, set())
            if not gained:
                continue
            if key not in nodes:
                # An invocation the baseline never made: a function this one
                # would have deleted survives, and every pass now runs on it.
                cls = next((r.cls for r in trace.runs if r.key == key), key)
                name = next((r.name for r in trace.runs if r.key == key), cls)
                unit = next((r.unit for r in trace.runs if r.key == key), "")
                nodes[key] = Node(key, cls, name, unit, in_baseline=False)
            edges.append(Edge(cause, key, PREEMPTS, tuple(sorted(gained))))
    return CausalGraph(
        nodes=nodes,
        edges=_mark_direct(edges, nodes),
        ablated=len(ablations),
        candidates=len(candidates_of(baseline)) if candidates is None else candidates,
        baseline_cmd=baseline.cmd,
    )


def analyze(
    toolchain: Toolchain,
    input_ir: str | Path,
    passes: str,
    *,
    tracker: Path | None = None,
    load_pass_plugins: Sequence[str] = (),
    extra_args: Sequence[str] = (),
    timeout: float | None = None,
    limit: int | None = DEFAULT_LIMIT,
    jobs: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> CausalGraph:
    """Baseline once, then one ablated run per invocation that changed the IR."""
    input_ir = Path(input_ir)
    tracker = tracker or build_tracker(toolchain)
    common = dict(load_pass_plugins=load_pass_plugins, extra_args=extra_args,
                  timeout=timeout)
    baseline = run_trace(toolchain, tracker, input_ir, passes, **common)
    if baseline.failed:
        raise CausalityError("opt failed under the provenance tracker:\n"
                             + baseline.stderr_tail)
    if not baseline.runs:
        raise CausalityError("the provenance tracker logged nothing; "
                             "was the plugin built for this opt?")
    every = candidates_of(baseline)
    chosen = every if limit is None else every[:limit]

    done = 0

    def ablate(key: str) -> tuple[str, Trace]:
        nonlocal done
        trace = run_trace(toolchain, tracker, input_ir, passes, skip=(key,), **common)
        done += 1
        if progress:
            progress(done, len(chosen))
        return key, trace

    with ThreadPoolExecutor(max_workers=jobs or os.cpu_count() or 2) as pool:
        ablations = dict(pool.map(ablate, chosen))
    return build_graph(baseline, ablations, candidates=len(every))


# --- onto the report's lane A -------------------------------------------------


def align_ords(graph: CausalGraph, pass_runs: Iterable[Any]) -> dict[int, int]:
    """Tracker ordinal -> lane-A run index (PassRun.index).

    Both number the same invocations in the same order, but the lane-A parser
    only keeps pass names without spaces (`RequireAnalysisPass<a, b>` is lost),
    so the tracker's sequence is walked as a supersequence of the parser's.
    A name that disagrees means the two runs were not the same pipeline, and
    no mapping is better than a wrong one.
    """
    ords = sorted((o, node.cls) for node in graph.nodes.values()
                  if node.in_baseline for o in node.ords)
    # The module's writer depends on -S, not on the pipeline being traced.
    runs = [run for run in pass_runs if run.name not in OUTPUT_WRITERS]
    mapping: dict[int, int] = {}
    j = 0
    for ord_, cls in ords:
        if re.search(r"\s", cls) or cls in OUTPUT_WRITERS:
            continue
        if j >= len(runs) or runs[j].name != cls:
            return {}
        mapping[ord_] = runs[j].index
        j += 1
    return mapping if j == len(runs) else {}


def causality_document(graph: CausalGraph, run_of: dict[int, int]) -> dict[str, Any]:
    """The report's data/causality.json: nodes by position, edges between them."""
    keys = sorted(graph.nodes, key=lambda k: (not graph.nodes[k].in_baseline,
                                              min(graph.nodes[k].ords or [1 << 30]), k))
    touched = {e.cause for e in graph.edges} | {e.effect for e in graph.edges}
    # Only what an edge or an ablation says anything about: the other ~80% of
    # invocations changed nothing and caused nothing.
    keys = [k for k in keys if k in touched or graph.nodes[k].changed_ords
            or graph.nodes[k].error]
    index = {key: i for i, key in enumerate(keys)}

    def runs(ords: Iterable[int]) -> list[int]:
        return [run_of[o] for o in ords if o in run_of]

    nodes = []
    for key in keys:
        node = graph.nodes[key]
        nodes.append({
            "key": key,
            "pass": node.cls,
            "name": node.name,
            "unit": node.unit,
            "runs": runs(node.ords),
            "changedRuns": runs(node.changed_ords),
            "entities": node.entities,
            "changed": bool(node.changed_ords),
            "finalDiffers": node.final_differs,
            "inBaseline": node.in_baseline,
            "error": node.error,
        })
    edges = [
        {"from": index[e.cause], "to": index[e.effect], "kind": e.kind,
         "fns": list(e.entities), "direct": e.direct}
        for e in graph.edges if e.cause in index and e.effect in index
    ]
    return {
        "lane": "ir",
        "ablated": graph.ablated,
        "candidates": graph.candidates,
        "truncated": graph.truncated,
        "aligned": bool(run_of),
        "nodes": nodes,
        "edges": edges,
    }


# --- as text ------------------------------------------------------------------


def _label(node: Node) -> str:
    where = node.unit.replace("function ", "")
    at = f"#{min(node.changed_ords or node.ords)}" if node.ords else "(new)"
    return f"{at} {node.name} on {where}"


def chains(graph: CausalGraph, max_chains: int = 25) -> list[list[str]]:
    """The longest direct enabling paths, each ending where no edge leaves it."""
    out_of: dict[str, list[str]] = {}
    has_parent: set[str] = set()
    for edge in graph.edges:
        if edge.kind == ENABLES and edge.direct:
            out_of.setdefault(edge.cause, []).append(edge.effect)
            has_parent.add(edge.effect)
    memo: dict[str, list[str]] = {}

    def longest(key: str) -> list[str]:
        if key not in memo:
            best: list[str] = []
            for nxt in out_of.get(key, ()):
                path = longest(nxt)
                if len(path) > len(best):
                    best = path
            memo[key] = [key] + best
        return memo[key]

    roots = [k for k in out_of if k not in has_parent]
    paths = sorted((longest(r) for r in roots), key=len, reverse=True)
    return [p for p in paths if len(p) > 1][:max_chains]


def render_text(graph: CausalGraph, only: str | None = None) -> str:
    lines = [f"ablated {graph.ablated} of {graph.candidates} invocations that changed the IR"
             + (" (limit reached)" if graph.truncated else "")]
    enables = [e for e in graph.edges if e.kind == ENABLES]
    lines.append(f"{len(enables)} enabling edges "
                 f"({sum(e.direct for e in enables)} direct), "
                 f"{len(graph.edges) - len(enables)} pre-empting")
    match = (lambda n: only.lower() in (n.name + " " + n.cls).lower()) if only else None

    lines.append("\nchains (each pass enables the next):")
    for path in chains(graph):
        if match and not any(match(graph.nodes[k]) for k in path):
            continue
        lines.append("  " + "\n    -> ".join(_label(graph.nodes[k]) for k in path))
    lines.append("\nper invocation:")
    by_cause: dict[str, list[Edge]] = {}
    for edge in graph.edges:
        by_cause.setdefault(edge.cause, []).append(edge)
    for key, node in sorted(graph.nodes.items(), key=lambda kv: min(kv[1].ords or [1 << 30])):
        if not node.changed_ords or (match and not match(node)):
            continue
        final = {True: "final IR differs without it", False: "redundant: final IR identical without it",
                 None: "not ablated"}[node.final_differs]
        lines.append(f"  {_label(node)}  [{final}]")
        if node.error:
            lines.append(f"      ! {node.error.splitlines()[0]}")
        for edge in by_cause.get(key, ()):
            verb = "enables " if edge.kind == ENABLES else "pre-empts"
            mark = "" if edge.direct else " (via others)"
            lines.append(f"      {verb} {_label(graph.nodes[edge.effect])}"
                         f" [{', '.join(edge.entities)}]{mark}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m llvm_lens.causality",
        description="Ablate each pass invocation that changed the IR, one at a "
                    "time, and print which later invocations it enabled or "
                    "pre-empted.",
    )
    parser.add_argument("input_ir", help="LLVM IR (.ll/.bc) to run opt on")
    parser.add_argument("--passes", default="default<O2>")
    parser.add_argument("--bin-dir", default=None)
    parser.add_argument("--llvm-version", type=int, default=None)
    parser.add_argument("--load-pass-plugin", action="append", default=[], metavar="SO")
    parser.add_argument("--opt-arg", action="append", default=[], metavar="ARG")
    parser.add_argument("--plugin", default=None, help="prebuilt tracker .so")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--only", default=None, metavar="PASS",
                        help="only show chains and invocations naming this pass")
    args = parser.parse_args(argv)

    toolchain = discover_toolchain(args.bin_dir, args.llvm_version)
    tracker = Path(args.plugin) if args.plugin else build_tracker(toolchain)

    def progress(done: int, total: int) -> None:
        print(f"\rablating {done}/{total}", end="", file=sys.stderr, flush=True)

    graph = analyze(toolchain, args.input_ir, args.passes, tracker=tracker,
                    load_pass_plugins=args.load_pass_plugin, extra_args=args.opt_arg,
                    timeout=args.timeout, limit=args.limit, jobs=args.jobs,
                    progress=progress)
    print(file=sys.stderr)
    print("baseline: " + shlex.join(graph.baseline_cmd))
    print(render_text(graph, args.only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
