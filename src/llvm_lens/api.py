"""Programmatic API: the IR, or machine IR, around a named pass.

    snap = llvm_lens.snapshot("a.c", "greedy", when="after")
    snap.functions["main"]        # verbatim MIR for main, after regalloc

Name a pass by the id LLVM prints in parentheses ("greedy", "x86-isel"), not by
its display name. Neither tool treats an unknown name as an error — it exits 0
having dumped nothing — so a name that matched no dump is raised here rather
than returned as an empty answer. A pass that ran more than once keeps every run
in ``PassSnapshot.runs``.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .compile import compile_to_ir
from .parsers.mir import parse_ir_dumps, parse_mir_snapshots, split_machine_functions
from .parsers.print_changed import parse_direct_ir, split_module_functions
from .runner_llc import run_llc
from .runner_opt import run_opt
from .settings import DEFAULT_PASSES
from .toolchain import Toolchain, discover_toolchain

__all__ = [
    "PassSnapshot", "SnapshotError", "SnapshotRun", "list_machine_passes",
    "machine_ir", "snapshot",
]

LANES = ("machine", "ir")
WHENS = ("before", "after")

# The name of the subdirectory holding captures, under the caller's out_dir.
RAW_DIR = "raw"

# What list_machine_passes() compiles when the caller names no source file
SCRATCH_SOURCE = """\
/* Compiled when llvm_lens.list_machine_passes() is given no source. */
int square(int x) { return x * x; }

int sum_squares(const int *values, int n) {
    int total = 0;
    for (int i = 0; i < n; i++)
        total += square(values[i]);
    return total;
}
"""


class SnapshotError(RuntimeError):
    """The requested pass, lane, or occurrence could not be answered."""


@dataclass(frozen=True)
class SnapshotRun:
    """One run of the pass: the state it saw, or the state it left.

    Quote ``functions`` rather than slicing ``text``: a machine pass dumps one
    function at a time, so a repeated pass's runs interleave in the capture.
    """

    index: int  # 0-based position among the runs, in pipeline order
    text: str  # this run's dumps, verbatim, concatenated
    functions: dict[str, str] = field(default_factory=dict)  # fn -> verbatim text
    line: int = 0  # 1-based line of this run's first dump in the capture


@dataclass(frozen=True)
class PassSnapshot:
    """The IR or MIR around one pass, plus every run behind it.

    ``functions`` is verbatim per-function text, in the order the dump listed
    it, and ``format`` says which language it is in — llc runs IR passes too.
    The IR lane keeps -print-module-scope, so ``.text`` is the whole module.

    ``functions`` is empty, with the state still in ``text``, when a dump names
    no function: llc's legacy loop passes print a loop, and its IR headers carry
    no function name.
    """

    lane: str  # "machine" | "ir"
    format: str  # "mir" | "ir" — what `functions` holds
    pass_name: str  # display name as LLVM printed it
    pass_id: str  # the id the pass was named by
    when: str  # "before" | "after"
    occurrence: int  # which run text/functions resolved to
    text: str  # that run's dumps, verbatim (see SnapshotRun.text)
    functions: dict[str, str] = field(default_factory=dict)  # fn -> verbatim text
    runs: tuple[SnapshotRun, ...] = ()
    cmd: tuple[str, ...] = ()
    stderr_path: Path | None = None

    @property
    def function_names(self) -> list[str]:
        """The functions this run covers, in the order the dump listed them."""
        return list(self.functions)


@dataclass(frozen=True)
class _Captured:
    """One dump from a capture, before runs are grouped out of them."""

    pass_name: str
    pass_id: str
    text: str
    line: int
    functions: dict[str, str]


# --- reading a capture --------------------------------------------------------


def _machine_captures(stderr: str, when: str) -> tuple[list[_Captured], str]:
    """MIR dumps for *when*; falling back to llc's IR-level dumps.

    llc runs IR passes before codegen and dumps those in IR form, so a pass
    named from that half of the pipeline comes back as IR, not MIR.
    """
    captures: list[_Captured] = []
    for snap in parse_mir_snapshots(stderr):
        if snap.when != when:
            continue
        functions = split_machine_functions(snap.text)
        # A functionless machine dump means a header with nothing under it, not
        # a state to hand back, so it falls through to the IR dumps and, failing
        # those, to the no-match error.
        if functions:
            captures.append(
                _Captured(snap.pass_name, snap.pass_id, snap.text, snap.line, functions)
            )
    if captures:
        return captures, "mir"

    for dump in parse_ir_dumps(stderr):
        if dump.when != when:
            continue
        captures.append(_Captured(dump.pass_name, dump.pass_id, dump.text,
                                  dump.line, split_module_functions(dump.text)))
    return captures, "ir"


def _ir_captures(stderr: str, when: str, asked: str) -> tuple[list[_Captured], str]:
    """Whole-module IR dumps for *when*, one per dump.

    The header names the pass class (InstCombinePass), not the name it was asked
    for by, so *asked* is what the result reports as the id.
    """
    captures = [
        _Captured(snap.pass_name, asked, snap.raw, snap.line,
                  split_module_functions(snap.raw))
        for snap in parse_direct_ir(stderr)
        if snap.when == when
    ]
    return captures, "ir"


def _print_flag(pass_name: str, when: str) -> dict[str, tuple[str, ...]]:
    """The -print-before/-print-after keyword for *when*, as a kwarg pair."""
    return {"print_before" if when == "before" else "print_after": (pass_name,)}


def _group_machine(captures: list[_Captured]) -> list[SnapshotRun]:
    """Group machine dumps by the pipeline position that produced them.

    llc's legacy pass manager runs one function at a time, so a pass appearing
    twice in the pipeline dumps each function twice in a row. Counting how often
    a function has been dumped recovers the position; grouping by adjacency does
    not, and would cut runs covering different function sets.

    A dump naming no function (llc's loop passes print a loop) cannot be matched
    to another, so it stands alone.
    """
    groups: list[list[_Captured]] = []
    by_position: dict[int, list[_Captured]] = {}
    seen: dict[str, int] = {}
    for capture in captures:
        if not capture.functions:
            groups.append([capture])
            continue
        position = max(seen.get(name, 0) for name in capture.functions)
        group = by_position.get(position)
        if group is None:
            group = by_position[position] = []
            groups.append(group)
        group.append(capture)
        for name in capture.functions:
            seen[name] = position + 1

    return [
        SnapshotRun(
            index,
            "\n".join(dump.text for dump in group),
            {fn: text for dump in group for fn, text in dump.functions.items()},
            group[0].line,
        )
        for index, group in enumerate(groups)
    ]


def _group_ir(captures: list[_Captured]) -> list[SnapshotRun]:
    """One run per dump: each is the whole module at one moment."""
    return [
        SnapshotRun(index, capture.text, dict(capture.functions), capture.line)
        for index, capture in enumerate(captures)
    ]


def _no_match(pass_name: str, lane: str, when: str, passes: str | None) -> SnapshotError:
    if lane == "machine":
        return SnapshotError(
            f"no machine pass named {pass_name!r} dumped anything ({when}) in the "
            "llc backend pipeline.\n"
            "-print-before/-print-after take the id LLVM prints in parentheses, "
            "not the display name: the register allocator is 'greedy' on x86-64, "
            "instruction selection 'x86-isel' (the target's own id, so aarch64 "
            "differs). A pass that ran on no function — one carrying optnone, or "
            "a pass this pipeline skips — also dumps nothing.\n"
            "llvm-lens.list_machine_passes(source) lists the ids this file runs."
        )
    # opt's class names look like plausible ids — no space to reject on — and
    # -passes takes the name the class wraps instead.
    if pass_name.endswith("Pass") or any(ch.isupper() for ch in pass_name):
        hint = (
            f"\n{pass_name!r} looks like opt's class name; give the name -passes "
            f"takes for it, e.g. 'instcombine', 'mem2reg'."
        )
    else:
        hint = ""
    return SnapshotError(
        f"no pass named {pass_name!r} dumped anything ({when}) in pipeline "
        f"{passes!r}.\n"
        "Name a pass as opt's -passes writes it, e.g. 'instcombine', 'mem2reg', "
        "'gvn'. A pass that changed nothing still dumps on this path, but one the "
        "pipeline never reaches does not." + hint
    )


def _check_pass_name(pass_name: str, lane: str) -> None:
    """Reject what cannot be a single pass id before spending a subprocess.

    Both tools take a comma-separated list here, and a bad value would otherwise
    mean "dumped nothing" — an error neither tool raises.
    """
    if not pass_name.strip():
        raise SnapshotError("pass_name is empty; name the pass to snapshot, e.g. 'greedy'")
    if "," in pass_name:
        raise SnapshotError(
            f"pass_name must be a single pass id, got {pass_name!r}; ask about "
            "one pass at a time rather than a -print-... list."
        )
    if any(ch.isspace() for ch in pass_name):
        hint = (
            " Use the id LLVM prints in parentheses instead; "
            "list_machine_passes(source) prints the pairs."
            if lane == "machine" else
            " Use the name opt's -passes takes, e.g. 'instcombine'."
        )
        raise SnapshotError(
            f"pass_name looks like a display name, not an id: {pass_name!r}.{hint}"
        )


def _run_dir(out_dir: str | Path | None) -> Path:
    """The caller's directory, else a fresh temporary one: a library call does
    not write into the working directory the way the CLI does."""
    if out_dir is not None:
        return Path(out_dir)
    return Path(tempfile.mkdtemp(prefix="llvm-lens-"))


def _scratch_source() -> Path:
    scratch = Path(tempfile.mkdtemp(prefix="llvm-lens-src-"))
    path = scratch / "scratch.c"
    path.write_text(SCRATCH_SOURCE)
    return path


# --- the public surface -------------------------------------------------------


def snapshot(
    source: str | Path,
    pass_name: str,
    *,
    when: Literal["before", "after"] = "after",
    lane: Literal["machine", "ir"] = "machine",
    passes: str | None = None,
    occurrence: int | None = None,
    out_dir: str | Path | None = None,
    bin_dir: str | Path | None = None,
    llvm_version: int | None = None,
    target: str | None = None,
    timeout: float | None = None,
    toolchain: Toolchain | None = None,
) -> PassSnapshot:
    """Return the IR (or machine IR) around *pass_name*, run on *source*.

    *lane* picks the half of the compiler: "machine" runs llc over clang -O0 IR,
    "ir" runs opt over it, and then *passes* is the pipeline to run (default:
    default<O2>).

    *occurrence* selects which run of a pass that ran repeatedly becomes ``text``
    and ``functions``; it defaults to the first run for "before" and the last
    for "after", and every run stays available in ``runs``.

    Captures are written under *out_dir*, a fresh temporary directory when it is
    None; two calls sharing one would overwrite each other's capture.
    """
    if when not in WHENS:
        raise SnapshotError(f"when must be one of {', '.join(WHENS)}, not {when!r}")
    if lane not in LANES:
        raise SnapshotError(f"lane must be one of {', '.join(LANES)}, not {lane!r}")
    if lane == "machine" and passes is not None:
        raise SnapshotError(
            "passes= selects an opt pipeline, which only lane='ir' runs; the "
            "machine lane shows a machine pass over clang's unoptimized IR"
        )
    if lane == "ir" and passes is not None and not passes.strip():
        raise SnapshotError("passes= is empty; give a pipeline, e.g. 'default<O2>'")
    _check_pass_name(pass_name, lane)

    source = Path(source)
    if toolchain is None:
        toolchain = discover_toolchain(bin_dir, llvm_version)

    raw = _run_dir(out_dir) / RAW_DIR
    raw.mkdir(parents=True, exist_ok=True)
    clang_extra = ("-target", target) if target else ()
    compiled = compile_to_ir(
        source, toolchain=toolchain, out_dir=raw, timeout=timeout,
        extra_args=clang_extra,
    )

    if lane == "machine":
        llc_extra = ("-mtriple", target) if target else ()
        result = run_llc(
            compiled.ir_path, out_dir=raw, print_all=False,
            extra_args=llc_extra, timeout=timeout, toolchain=toolchain,
            **_print_flag(pass_name, when),
        )
    else:
        result = run_opt(
            toolchain, compiled.ir_path, passes or DEFAULT_PASSES, out_dir=raw,
            print_all=False, timeout=timeout, **_print_flag(pass_name, when),
        )
    stderr_path = result.stderr_path
    cmd = result.cmd

    if result.failed:
        raise SnapshotError(
            f"the {lane} lane failed (exit {result.returncode}); "
            f"see {stderr_path}\n{_tail(stderr_path)}"
        )

    stderr = stderr_path.read_text(errors="replace")
    if lane == "machine":
        captures, fmt = _machine_captures(stderr, when)
        runs = _group_machine(captures)
        pipeline = None
    else:
        pipeline = passes or DEFAULT_PASSES
        captures, fmt = _ir_captures(stderr, when, pass_name)
        runs = _group_ir(captures)

    if not captures:
        raise _no_match(pass_name, lane, when, pipeline)

    if occurrence is None:
        occurrence = 0 if when == "before" else len(runs) - 1
    if not 0 <= occurrence < len(runs):
        raise SnapshotError(
            f"found {len(runs)} run(s) of {pass_name!r} in this pipeline; "
            f"occurrence must be 0..{len(runs) - 1}"
        )

    chosen = runs[occurrence]
    first = captures[0]
    return PassSnapshot(
        lane=lane,
        format=fmt,
        pass_name=first.pass_name,
        pass_id=first.pass_id,
        when=when,
        occurrence=occurrence,
        text=chosen.text,
        functions=chosen.functions,
        runs=tuple(runs),
        cmd=tuple(cmd),
        stderr_path=stderr_path,
    )


def list_machine_passes(
    source: str | Path | None = None,
    *,
    out_dir: str | Path | None = None,
    bin_dir: str | Path | None = None,
    llvm_version: int | None = None,
    target: str | None = None,
    timeout: float | None = None,
    toolchain: Toolchain | None = None,
) -> list[tuple[str, str]]:
    """Every pass llc runs on *source*, as (id, display name), in pipeline order.

    *source* is optional: with no file named, a small scratch C file is compiled
    instead, written under /tmp

    Runs the whole backend with -print-after-all, so it costs a full llc run
    rather than a targeted one; the ids it returns are what snapshot() accepts.
    """
    if toolchain is None:
        toolchain = discover_toolchain(bin_dir, llvm_version)
    source = Path(source) if source is not None else _scratch_source()

    raw = _run_dir(out_dir) / RAW_DIR
    raw.mkdir(parents=True, exist_ok=True)
    clang_extra = ("-target", target) if target else ()
    compiled = compile_to_ir(
        source, toolchain=toolchain, out_dir=raw, timeout=timeout,
        extra_args=clang_extra,
    )
    llc_extra = ("-mtriple", target) if target else ()
    result = run_llc(
        compiled.ir_path, out_dir=raw, extra_args=llc_extra, timeout=timeout,
        toolchain=toolchain,
    )
    if result.failed:
        raise SnapshotError(
            f"llc failed (exit {result.returncode}); see {result.stderr_path}"
        )

    stderr = result.stderr_path.read_text(errors="replace")
    seen: dict[str, str] = {}
    for snap in parse_mir_snapshots(stderr):
        seen.setdefault(snap.pass_id, snap.pass_name)
    for dump in parse_ir_dumps(stderr):
        seen.setdefault(dump.pass_id, dump.pass_name)
    return list(seen.items())


def machine_ir(
    source: str | Path | None = None,
    *,
    stop_after: str | None = None,
    stop_before: str | None = None,
    simplify: bool = False,
    out_dir: str | Path | None = None,
    bin_dir: str | Path | None = None,
    llvm_version: int | None = None,
    target: str | None = None,
    timeout: float | None = None,
    toolchain: Toolchain | None = None,
) -> str:
    """The machine IR the backend holds when it stops at a pass, as text.
    """
    if (stop_after is None) == (stop_before is None):
        raise SnapshotError(
            "give exactly one of stop_after= or stop_before=, naming the pass "
            "to stop at; list_machine_passes(source) prints the ids this file "
            "runs"
        )
    stop_id = stop_after if stop_after is not None else stop_before
    _check_pass_name(str(stop_id), "machine")

    if toolchain is None:
        toolchain = discover_toolchain(bin_dir, llvm_version)
    source = Path(source) if source is not None else _scratch_source()

    raw = _run_dir(out_dir) / RAW_DIR
    raw.mkdir(parents=True, exist_ok=True)
    clang_extra = ("-target", target) if target else ()
    compiled = compile_to_ir(
        source, toolchain=toolchain, out_dir=raw, timeout=timeout,
        extra_args=clang_extra,
    )
    llc_extra = ("-mtriple", target) if target else ()
    result = run_llc(
        compiled.ir_path, out_dir=raw, print_all=False,
        stop_after=stop_after or "", stop_before=stop_before or "",
        simplify_mir=simplify, extra_args=llc_extra, timeout=timeout,
        toolchain=toolchain,
    )
    if result.failed:
        raise SnapshotError(
            f"llc failed (exit {result.returncode}); see {result.stderr_path}\n"
            f"{_tail(result.stderr_path)}"
        )
    if result.asm_path is None:
        raise SnapshotError(
            f"llc stopped at {stop_id!r} having written no MIR; "
            f"see {result.stderr_path}"
        )
    return result.asm_path.read_text(errors="replace")


def _tail(path: Path, lines: int = 20) -> str:
    """The end of a capture, for an error message."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "(no capture)"
    return "\n".join(text.splitlines()[-lines:])
