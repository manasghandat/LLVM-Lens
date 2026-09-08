"""Pipeline assembly: compile -> opt lane -> llc lane -> emit HTML report."""

from __future__ import annotations

import datetime as _dt
import re
import shlex
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from .cfg import ir_cfg_dot, machine_cfg_dot
from .compile import CompiledSource, compile_to_ir
from .diff import FnChange
from .emit import ReportPass, emit_report
from .parsers.debug_pass_manager import parse_pass_runs
from .parsers.legacy_pass_structure import PassNode, build_tree, parse_pass_structure
from .parsers.mir import parse_mir_snapshots, vreg_to_physreg
from .parsers.print_changed import (
    parse_changed_ir, split_module_functions, strip_module_noise,
)
from .parsers.time_passes import parse_time_passes
from .runner_llc import LlcResult, run_llc
from .runner_opt import OptResult, run_opt
from .sourcemap import (
    MIR_REF_RE, DebugTable, LineMap, encode, harvest_mir_table, has_debug_info,
    map_lines, parse_debug_table, read_sources,
)
from .toolchain import Toolchain, discover_toolchain

IR_DUMP_HEADER_RE = re.compile(r"^\*\*\* IR Dump After ")
MACHINE_HEADER_RE = re.compile(r"^# \*\*\* IR Dump After ")

DEFAULT_PASSES = "default<O2>"

# opt names a whole-module dump "[module]".
MODULE_FN = "[module]"
# Synthetic cards holding the IR each lane was handed; neither is a pass.
INPUT_PASS_NAME = "Input IR"
BACKEND_INPUT_PASS_NAME = "Optimized IR"
# A single-function SCC "(main)" and a loop fold into their function; "(a, b)" stays its own entity.
SCC_FN_RE = re.compile(r"^\(([^(),]+)\)$")
LOOP_FN_RE = re.compile(r"^loop .* in function (.+)$")

# opt driver passes clang never runs; omitted to match -fdebug-pass-structure.
OPT_DRIVER_PASSES = frozenset({"VerifierPass", "PrintModulePass"})


# --- lane builders -----------------------------------------------------------


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _normalize_pass_name(name: str) -> str:
    """Case/punctuation-insensitive key for --custom-pass badging."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _is_custom(name: str | None, custom_passes: tuple[str, ...]) -> bool:
    """Whether *name* was declared via --custom-pass (case-insensitive)."""
    if not name or not custom_passes:
        return False
    key = _normalize_pass_name(name)
    return any(key and key == _normalize_pass_name(c) for c in custom_passes)


def _effective_pipeline(passes: str, custom_passes: tuple[str, ...]) -> str:
    """Append --custom-pass names as deduped ``function(<name>)`` passes."""
    effective = passes
    existing = {token.strip() for token in effective.split(",")}
    for name in custom_passes:
        if name in existing:
            continue
        element = f"function({name})"
        if element in existing:
            continue
        effective = f"{effective},{element}"
        existing.add(element)
    return effective


def _pass_log(stderr: str, start_line: int, end_line: int) -> str:
    """Lines of a pass's stderr slice, with the IR dump bodies stripped."""
    kept: list[str] = []
    skipping = False
    for line in stderr.splitlines()[start_line - 1 : end_line - 1]:
        if IR_DUMP_HEADER_RE.match(line):
            skipping = True
            continue
        if line.startswith("Running pass:"):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _mir_log(stderr: str, start_line: int, end_line: int) -> str:
    """Lines of a machine pass's stderr slice, MIR blocks stripped."""
    kept: list[str] = []
    skipping = False
    for line in stderr.splitlines()[start_line - 1 : end_line - 1]:
        if line.startswith("# Machine code for function") or line.startswith("# End machine code"):
            skipping = True
            continue
        if MACHINE_HEADER_RE.match(line):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


def _summary_times_ms(time_blocks: list[Any]) -> dict[str, float]:
    """Pass name -> ms from the final \"Pass execution timing report\" table."""
    table: dict[str, float] = {}
    for block in time_blocks:
        if not block.is_summary:
            continue
        for name, seconds in block.rows:
            table[name] = seconds * 1000.0
    return table


def _attribute_time_ms(
    time_blocks: list[Any],
    anchors: list[int],
) -> dict[int, float]:
    """Total non-summary seconds per pass, by preceding anchor line."""
    totals: dict[int, float] = {}
    for block in time_blocks:
        if block.is_summary:
            continue
        anchor = max((i for i, line in enumerate(anchors) if line < block.line), default=None)
        if anchor is not None:
            totals[anchor] = totals.get(anchor, 0.0) + block.total_seconds * 1000.0
    return totals


def _canonical_fn(name: str) -> str:
    """The entity a dump belongs to, as the report tracks it."""
    match = SCC_FN_RE.match(name) or LOOP_FN_RE.match(name)
    return match.group(1) if match else name


def _entity_text(entity: str, body: str) -> str:
    """The part of one module-scope dump body that belongs to *entity*."""
    if entity == MODULE_FN:
        return body
    bodies = split_module_functions(body)
    if not bodies:
        return body
    if entity.startswith("(") and entity.endswith(")"):
        members = [name.strip() for name in entity[1:-1].split(",")]
        chosen = [bodies[name] for name in members if name in bodies]
        return "\n\n".join(chosen) if chosen else body
    return bodies.get(entity, body)


def build_lane_a(
    stderr: str,
    custom_passes: tuple[str, ...] = (),
    input_ir: str | None = None,
    mapped: bool = True,
) -> list[ReportPass]:
    """Assemble Lane A (opt) passes from captured stderr."""
    from .parsers.debug_pass_manager import scope_of

    runs = parse_pass_runs(stderr)
    dumps = parse_changed_ir(stderr)
    time_blocks = parse_time_passes(stderr)

    order: list[str] = []
    first_run_lines: list[int] = []  # aligned with `order`
    # Pass name -> pass-manager scope; the first run's scope wins.
    scope_by_name: dict[str, str] = {}
    for run in runs:
        if run.name not in order:
            order.append(run.name)
            first_run_lines.append(run.line)
            scope_by_name[run.name] = scope_of(run.function)

    snap: dict[tuple[str, str], str] = {}
    snap_src: dict[tuple[str, str], LineMap] = {}
    fn_order: dict[str, int] = {}
    for dump in dumps:
        key = (dump.pass_name, _canonical_fn(dump.function))
        fn_order.setdefault(key[1], len(fn_order))
        if key in snap:
            continue
        snap[key] = _entity_text(key[1], dump.ir)
        table = parse_debug_table(dump.metadata) if mapped and dump.metadata else None
        if table:
            snap_src[key] = map_lines(snap[key], table)

    analyses: dict[str, dict[str, list[str]]] = {}
    for run in runs:
        bucket = analyses.setdefault(run.name, {"run": [], "cached": [], "invalidated": []})
        for event in run.analyses:
            bucket["cached" if event.cached else "run"].append(f"{event.name} on {event.function}")
        for event in run.invalidated:
            bucket["invalidated"].append(f"{event.name} on {event.function}")

    summary_ms = _summary_times_ms(time_blocks)
    anchor_ms = _attribute_time_ms(time_blocks, first_run_lines)

    passes: list[ReportPass] = []
    # Last known text/CFG of each dumped entity, keyed the way opt names it.
    prev_text: dict[str, str] = {}
    prev_dots: dict[str, str | None] = {}
    # Functions the module currently holds, in module order.
    known_fns: list[str] = []
    if input_ir is not None:
        prev_text[MODULE_FN] = input_ir
        prev_dots[MODULE_FN] = ir_cfg_dot(input_ir, MODULE_FN)
        for fn, text in split_module_functions(input_ir).items():
            prev_text[fn] = text
            prev_dots[fn] = ir_cfg_dot(text, fn)
            known_fns.append(fn)
    line_count = len(stderr.splitlines()) + 1
    for run_index, name in enumerate(order, start=1):
        fn_changes: dict[str, FnChange] = {}
        src_maps: dict[str, LineMap] = {}
        for fn in sorted(fn_order, key=fn_order.get):
            after = snap.get((name, fn))
            if after is None:
                continue
            fn_changes[fn] = FnChange(fn, prev_text.get(fn, ""), after)
            after_src = snap_src.get((name, fn))
            if after_src is not None:
                src_maps[fn] = after_src
        # A module/SCC dump also carries the new state of every function inside it.
        bodies: dict[str, str] = {}
        for fn, change in fn_changes.items():
            prev_text[fn] = change.after
            bodies.update(split_module_functions(change.after))
        prev_text.update(bodies)
        # Keep cards for passes with no dumps too; "changed only" hides them.
        if name in OPT_DRIVER_PASSES:
            continue

        first_run_line = first_run_lines[run_index - 1]
        end_line = first_run_lines[run_index] if run_index < len(first_run_lines) else line_count
        dots: dict[str, tuple[str | None, str | None]] = {}
        for fn, change in fn_changes.items():
            dots[fn] = (
                prev_dots.get(fn),
                ir_cfg_dot(change.after, fn) if change.changed else prev_dots.get(fn),
            )
        for fn, change in fn_changes.items():
            prev_dots[fn] = dots[fn][1]
        for fn, text in bodies.items():
            prev_dots[fn] = ir_cfg_dot(text, fn)

        # A module dump is authoritative about what the module holds.
        if MODULE_FN in fn_changes:
            known_fns = list(split_module_functions(fn_changes[MODULE_FN].after))

        # List every function on every card (changed=False for untouched ones).
        for fn in known_fns:
            text = prev_text.get(fn)
            if fn in fn_changes or not text:
                continue
            fn_changes[fn] = FnChange(fn, text, text)
            dots[fn] = (prev_dots.get(fn), prev_dots.get(fn))
        # Module, then functions in module order, then loop/SCC entities.
        ordered = [MODULE_FN] + known_fns + sorted(set(fn_changes) - {MODULE_FN} - set(known_fns))
        fn_changes = {fn: fn_changes[fn] for fn in ordered if fn in fn_changes}

        # Extend after the fill so a multi-function SCC is not listed twice.
        known_fns.extend(fn for fn in bodies if fn not in known_fns)

        passes.append(ReportPass(
            id=0,  # assigned by build_report
            lane="ir",
            name=name,
            pass_id=name,
            run_index=run_index,
            changed=any(c.changed for c in fn_changes.values()),
            functions=fn_changes,
            dots=dots,
            analyses=analyses.get(name, {"run": [], "cached": [], "invalidated": []}),
            log=_pass_log(stderr, first_run_line, end_line),
            time_ms=summary_ms.get(name, anchor_ms.get(run_index - 1)),
            is_custom=_is_custom(name, custom_passes),
            src_maps=src_maps,
            scope=scope_by_name.get(name),
        ))
    return passes


def build_input_pass(
    ir_text: str,
    origin: Path,
    *,
    lane: str = "ir",
    name: str = INPUT_PASS_NAME,
    note: str = "before any pass ran",
    mapped: bool = True,
) -> ReportPass:
    """The synthetic card holding the LLVM IR a lane was handed (run_index 0)."""
    table = parse_debug_table(ir_text) if mapped and has_debug_info(ir_text) else None
    text = strip_module_noise(ir_text.splitlines())
    return ReportPass(
        id=0,  # assigned by build_report
        lane=lane,
        name=name,
        pass_id=None,
        run_index=0,
        time_ms=None,
        changed=True,
        functions={MODULE_FN: FnChange(MODULE_FN, "", text)},
        dots={},  # no CFG: the view is withheld, so a graph would be dead weight
        analyses={"run": [], "cached": [], "invalidated": []},
        log=f"Module as it entered the {'backend' if lane == 'mir' else 'pipeline'}, "
            f"{note} ({origin}).",
        src_maps={MODULE_FN: map_lines(text, table)} if table else {},
        is_input=True,
    )


def _node(name: str, kind: str, *, depth: int = 0) -> dict[str, object]:
    return {"name": name, "kind": kind, "depth": depth, "passId": None, "children": []}


def _build_ir_tree(passes: list[ReportPass]) -> dict[str, object]:
    """Synthesize the new-PM pass-manager tree from each pass's scope."""
    root = _node("__root__", "root")
    sections = {
        "module": _node("Module", "group", depth=1),
        "cgscc": _node("CGSCC", "group", depth=1),
        "function": _node("Function", "group", depth=1),
        "loop": _node("Loop", "group", depth=1),
    }
    have = {k: False for k in sections}

    for pass_ in passes:
        if pass_.is_input:
            continue
        scope = pass_.scope or "function"
        if scope not in sections:
            scope = "function"
        have[scope] = True
        leaf: dict[str, object] = {
            "name": pass_.name, "kind": "pass",
            "depth": 2, "passId": pass_.id, "children": [],
        }
        sections[scope]["children"].append(leaf)  # type: ignore[union-attr]

    for key in ("module", "cgscc", "function", "loop"):
        if have[key]:
            root["children"].append(sections[key])  # type: ignore[union-attr]
    return root


def build_lane_b(
    stderr: str,
    asm_text: str | None = None,
    custom_passes: tuple[str, ...] = (),
    mir_table: DebugTable | None = None,
) -> tuple[list[ReportPass], list[PassNode], str | None]:
    """Assemble Lane B (llc) passes from captured stderr. Returns (passes, structure_nodes, pass_arguments)."""
    snapshots = parse_mir_snapshots(stderr)
    nodes, pass_arguments = parse_pass_structure(stderr)
    time_blocks = parse_time_passes(stderr)

    # Group snapshots by pass id, preserving first-seen (pipeline) order.
    order: list[str] = []
    by_id: dict[str, list[Any]] = {}
    for snapshot in snapshots:
        if snapshot.pass_id not in by_id:
            order.append(snapshot.pass_id)
        by_id.setdefault(snapshot.pass_id, []).append(snapshot)
    first_lines = [by_id[p][0].line for p in order]
    summary_ms = _summary_times_ms(time_blocks)
    anchor_ms = _attribute_time_ms(time_blocks, first_lines)

    # Per-function snapshot streams for before/after pairing.
    fn_seq: dict[str, list[tuple[str, Any]]] = {}
    for snapshot in snapshots:
        for fn in snapshot.functions:
            fn_seq.setdefault(fn, []).append((snapshot.pass_id, snapshot))

    passes: list[ReportPass] = []
    prev_mf: dict[str, Any] = {}
    line_count = len(stderr.splitlines()) + 1
    for run_index, pass_id in enumerate(order, start=1):
        group = by_id[pass_id]
        fn_changes: dict[str, FnChange] = {}
        dots: dict[str, tuple[str | None, str | None]] = {}
        spills: dict[str, int] = {}
        src_maps: dict[str, LineMap] = {}
        for snapshot in group:
            fn = next(iter(snapshot.functions))
            machine_function = snapshot.functions[fn]
            before_mf = prev_mf.get(fn)
            fn_changes[fn] = FnChange(
                fn,
                before_mf.text if before_mf else "",
                machine_function.text,
            )
            dots[fn] = (
                machine_cfg_dot(before_mf) if before_mf else None,
                machine_cfg_dot(machine_function),
            )
            spills[fn] = machine_function.spill_count
            if mir_table:
                src_maps[fn] = map_lines(machine_function.text, mir_table, MIR_REF_RE)
        for snapshot in group:
            fn = next(iter(snapshot.functions))
            prev_mf[fn] = snapshot.functions[fn]
        if not fn_changes:
            continue

        first_line = group[0].line
        end_line = (
            by_id[order[run_index]][0].line
            if run_index < len(order) else line_count
        )
        passes.append(ReportPass(
            id=0,
            lane="mir",
            name=group[0].pass_name,
            pass_id=pass_id,
            run_index=run_index,
            changed=any(c.changed for c in fn_changes.values()),
            functions=fn_changes,
            dots=dots,
            spills=spills,
            log=_mir_log(stderr, first_line, end_line),
            time_ms=summary_ms.get(group[0].pass_name, anchor_ms.get(run_index - 1)),
            is_custom=_is_custom(group[0].pass_name, custom_passes)
                      or _is_custom(pass_id, custom_passes),
            src_maps=src_maps,
        ))

    _attach_reg_maps(passes, order, by_id, fn_seq)
    if passes and asm_text:
        passes[-1].asm = asm_text
    return passes, nodes, pass_arguments


def _attach_reg_maps(
    passes: list[ReportPass],
    order: list[str],
    by_id: dict[str, list[Any]],
    fn_seq: dict[str, list[tuple[str, Any]]],
) -> None:
    """Attach vreg->physreg maps from the snapshots around VirtRegRewriter."""
    if "virtregrewriter" not in by_id:
        return
    rewriter_card = next((p for p in passes if p.pass_id == "virtregrewriter"), None)
    if rewriter_card is None:
        return
    for fn, sequence in fn_seq.items():
        seq_index = next(
            (i for i, (pass_id, _) in enumerate(sequence) if pass_id == "virtregrewriter"),
            None,
        )
        if seq_index is None:
            continue
        rewriter_snapshot = sequence[seq_index][1]
        pre = next(
            (s for pid, s in reversed(sequence[:seq_index]) if s.functions[fn].blocks),
            None,
        )
        if rewriter_snapshot.functions[fn].blocks:
            post = rewriter_snapshot
        else:
            post = next(
                (s for pid, s in sequence[seq_index + 1:] if s.functions[fn].blocks),
                None,
            )
        if pre is None or post is None:
            continue
        mapping = vreg_to_physreg(pre.functions[fn], post.functions[fn])
        if mapping:
            rewriter_card.reg_map[fn] = mapping


# --- pipeline -----------------------------------------------------------------


# Command-sheet stage title and note, keyed by CompiledSource.kind / lane.
COMMAND_TITLES = {
    "clang": ("compile", "source to LLVM IR"),
    "llvm-dis": ("compile", "bitcode to textual LLVM IR"),
    "passthrough": ("compile", "input is already textual LLVM IR; copied in"),
    "opt": ("opt", "middle-end pipeline (Lane A)"),
    "llc": ("llc", "backend pipeline (Lane B)"),
}


def build_commands(
    compiled: CompiledSource,
    opt_result: OptResult | None,
    llc_result: LlcResult | None,
) -> list[dict[str, Any]]:
    """The exact argv of every stage that ran, in run order."""
    stages: list[tuple[str, tuple[str, ...]]] = [(compiled.kind, compiled.cmd)]
    if opt_result is not None:
        stages.append(("opt", opt_result.cmd))
    if llc_result is not None:
        stages.append(("llc", llc_result.cmd))
    commands = []
    for kind, argv in stages:
        stage, note = COMMAND_TITLES.get(kind, (kind, ""))
        commands.append({
            "stage": stage,
            "note": note,
            "argv": list(argv),
            "line": shlex.join(argv),
        })
    return commands


def _attach_source_maps(passes: list[ReportPass]) -> list[dict[str, str]]:
    """Read every mapped source file and re-encode the maps against its index."""
    every: list[LineMap] = [
        mapping for pass_ in passes for mapping in pass_.src_maps.values()
    ]
    if not every:
        return []
    texts = read_sources(every)
    files = sorted(texts)
    for pass_ in passes:
        pass_.src_maps = {
            fn: encode(mapping, files) for fn, mapping in pass_.src_maps.items()
        }
    return [
        {"path": path, "name": Path(path).name, "text": texts[path]}
        for path in files
    ]


def default_frontend_dir() -> Path:
    """The packaged frontend assets, from a source checkout or an installed wheel."""
    return Path(files("llvm_lens") / "frontend")


def build_report(
    source: str | Path,
    passes: str = DEFAULT_PASSES,
    load_pass_plugins: tuple[str, ...] = (),
    load: tuple[str, ...] = (),
    custom_passes: tuple[str, ...] = (),
    output: str | Path = "report",
    bin_dir: str | Path | None = None,
    llvm_version: int | None = None,
    timeout: float | None = None,
    source_map: bool = True,
) -> dict[str, Any]:
    """Run the full pipeline and emit the report. Returns a summary dict."""
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    toolchain: Toolchain = discover_toolchain(bin_dir, llvm_version)

    effective_passes = _effective_pipeline(passes, custom_passes)

    started = time.perf_counter()
    compiled = compile_to_ir(source, toolchain=toolchain, out_dir=raw, timeout=timeout)
    opt_result = run_opt(
        toolchain, compiled.ir_path, effective_passes,
        out_dir=raw,
        load_pass_plugins=load_pass_plugins,
        print_after=custom_passes,
        timeout=timeout,
    )
    opt_stderr = opt_result.stderr_path.read_text(errors="replace")
    input_ir = compiled.ir_path.read_text(errors="replace")

    input_card = build_input_pass(input_ir, compiled.ir_path, mapped=source_map)
    lane_a = [input_card]

    mir_nodes: list[PassNode] = []
    pass_arguments: str | None = None
    if not opt_result.timed_out:
        lane_a += build_lane_a(
            opt_stderr, custom_passes,
            input_ir=input_card.functions[MODULE_FN].after,
            mapped=source_map,
        )

    lane_b: list[ReportPass] = []
    llc_result = None
    if not opt_result.failed and opt_result.ir_path is not None:
        lane_b.append(build_input_pass(
            opt_result.ir_path.read_text(errors="replace"), opt_result.ir_path,
            lane="mir", name=BACKEND_INPUT_PASS_NAME,
            note="after every opt pass", mapped=source_map,
        ))
        llc_result = run_llc(
            opt_result.ir_path, out_dir=raw,
            load_pass_plugins=load_pass_plugins, load=load,
            print_after=custom_passes,
            timeout=timeout, toolchain=toolchain,
        )
        if not llc_result.timed_out:
            llc_stderr = llc_result.stderr_path.read_text(errors="replace")
            asm_text = llc_result.asm_path.read_text(errors="replace") if llc_result.asm_path else None
            mir_table = harvest_mir_table(
                opt_result.ir_path, toolchain=toolchain,
                load=load, timeout=timeout,
            ) if source_map else None
            lane_b_passes, mir_nodes, pass_arguments = build_lane_b(
                llc_stderr, asm_text, custom_passes, mir_table,
            )
            lane_b += lane_b_passes
    total_ms = (time.perf_counter() - started) * 1000.0

    all_passes = lane_a + lane_b
    for index, pass_ in enumerate(all_passes, start=1):
        pass_.id = index

    mir_by_name = {p.name: p.id for p in lane_b}
    pipeline_tree = {
        "ir": _build_ir_tree(lane_a),
        "mir": build_tree(mir_nodes, mir_by_name),
    }

    source_files = _attach_source_maps(all_passes)

    final_cfg: dict[str, dict[str, str]] = {}
    for lane, lane_passes in (("ir", lane_a), ("mir", lane_b)):
        final: dict[str, str] = {}
        for pass_ in lane_passes:
            for fn, (_, after) in pass_.dots.items():
                if after:
                    final[fn] = after
        if final:
            final_cfg[lane] = final

    metadata = {
        "source": str(compiled.source_path),
        "inputKind": compiled.kind,
        "pipeline": effective_passes,
        "plugins": list(load_pass_plugins) + list(load),
        "customPasses": list(custom_passes),
        "commands": build_commands(compiled, opt_result, llc_result),
        "toolVersions": {name: tool.version for name, tool in toolchain.tools.items()},
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "totalTimeMs": round(total_ms, 1),
        "optCrashed": opt_result.failed,
        "llcCrashed": bool(llc_result and llc_result.failed),
        "finalCfg": final_cfg,
        "sourceFiles": source_files,
        "pipelineTree": pipeline_tree,
        "passArguments": pass_arguments,
        "errors": {},
    }
    if opt_result.failed:
        metadata["errors"]["opt"] = _tail(opt_stderr)
    if llc_result and llc_result.failed:
        metadata["errors"]["llc"] = _tail(llc_result.stderr_path.read_text(errors="replace"))

    manifest = emit_report(
        out, passes=all_passes, metadata=metadata,
        frontend_dir=default_frontend_dir(),
    )

    return {
        "reportDir": str(out.resolve()),
        "manifest": str(manifest),
        "laneACount": len(lane_a),
        "laneBCount": len(lane_b),
        "totalTimeMs": round(total_ms, 1),
        "optCrashed": opt_result.failed,
        "llcCrashed": bool(llc_result and llc_result.failed),
    }
