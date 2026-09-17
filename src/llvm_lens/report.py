"""Pipeline assembly: compile -> opt lane -> llc lane -> emit HTML report."""

from __future__ import annotations

import datetime as _dt
import re
import shlex
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from .analyses import compute_analyses
from .blame import (
    INPUT_NAME, Timeline, blame_document, canonical_fn, lane_a_slots,
    lane_a_timeline, mir_timeline,
)
from .cfg import ir_cfg_dot, machine_cfg_dot
from .compile import CompiledSource, compile_to_ir
from .config import load_config
from .diff import FnChange
from .emit import MODULE_FN, ReportPass, RunSegment, emit_report
from .asm import correlate_asm
from .isel import correlate
from .parsers.debug_pass_manager import parse_pass_runs
from .parsers.legacy_pass_structure import PassNode, analyses_by_pass, build_tree, parse_pass_structure
from .parsers.mir import IrDump, parse_ir_dumps, parse_mir_snapshots, vreg_to_physreg
from .parsers.print_changed import (
    parse_changed_ir, split_module_functions, strip_module_noise,
)
from .parsers.time_passes import parse_time_passes
from .runner_llc import LlcResult, run_llc
from .runner_opt import OptResult, run_opt
from .settings import DEFAULT_PASSES, Ui
from .sourcemap import (
    MIR_REF_RE, DebugTable, LineMap, encode, harvest_mir_table, has_debug_info,
    map_lines, parse_debug_table, read_sources,
)
from .toolchain import Toolchain, discover_toolchain

IR_DUMP_HEADER_RE = re.compile(r"^\*\*\* IR Dump After ")
MACHINE_HEADER_RE = re.compile(r"^# \*\*\* IR Dump After ")

__all__ = ["DEFAULT_PASSES", "build_report", "build_lane_a", "build_lane_b"]

INPUT_PASS_NAME = INPUT_NAME
BACKEND_INPUT_PASS_NAME = "Optimized IR"
OUTPUT_PASS_NAME = "Optimized MIR"


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


def lane_a_passes(
    stderr: str,
    custom_passes: tuple[str, ...] = (),
    input_ir: str | None = None,
    mapped: bool = True,
) -> tuple[list[ReportPass], Timeline]:
    """Lane A (opt): its cards, and the per-entity timeline behind them."""
    from .parsers.debug_pass_manager import scope_of

    runs = parse_pass_runs(stderr)
    dumps = parse_changed_ir(stderr)
    time_blocks = parse_time_passes(stderr)

    slots = lane_a_slots(runs, dumps)
    timeline = lane_a_timeline(slots, input_ir)

    analyses: dict[int, dict[str, list[str]]] = {}
    for run in runs:
        bucket = analyses.setdefault(run.index, {"run": [], "cached": [], "invalidated": []})
        for event in run.analyses:
            bucket["cached" if event.cached else "run"].append(f"{event.name} on {event.function}")
        for event in run.invalidated:
            bucket["invalidated"].append(f"{event.name} on {event.function}")

    summary_ms = _summary_times_ms(time_blocks)
    anchor_ms = _attribute_time_ms(time_blocks, [slot.run.line for slot in slots])
    # -time-passes measures a pass over the whole lane, never one run of it, so
    # its total hangs off the pass's first card. Repeating it on each card would
    # count the same milliseconds once per run wherever they are added up.
    timed: set[str] = set()

    passes: list[ReportPass] = []
    # Last CFG drawn for each entity; the DOT is a pure function of the text.
    prev_dots: dict[str, str | None] = {}
    # Last source map built for each entity, with the text it was built on: a
    # map is only good for that exact text, and other passes move it between
    # this pass's runs.
    prev_srcs: dict[str, tuple[str, LineMap]] = {}
    # Functions the module currently holds, in module order.
    known_fns: list[str] = []
    if input_ir is not None:
        known_fns = list(split_module_functions(input_ir))

    for index, slot in enumerate(slots):
        time_ms = None
        if slot.name not in timed:
            timed.add(slot.name)
            time_ms = summary_ms.get(slot.name, anchor_ms.get(index))

        # One card per pass, so its rows carry every run it made. Each run keeps
        # its own diff, CFG and source map; the card's own are the last one's,
        # which is the state it leaves behind.
        segments: list[RunSegment] = []
        fn_changes: dict[str, FnChange] = {}
        src_maps: dict[str, LineMap] = {}
        dots: dict[str, tuple[str | None, str | None]] = {}
        changed = False
        for state in slot.runs:
            run_index = state.run.index
            dumped = state.dumps[-1] if state.dumps else None
            # The module row is the whole-module overview, and only the passes
            # that ran on the module get one. Such a dump is also authoritative
            # about what the module holds.
            named_module = dumped is not None and canonical_fn(dumped.function) == MODULE_FN
            if named_module:
                known_fns = list(split_module_functions(dumped.ir))

            # Module, then functions in module order, then loop/SCC entities.
            listed = [MODULE_FN] if named_module else []
            listed += [fn for fn in known_fns if fn != MODULE_FN]
            listed += [fn for fn in sorted(timeline.changed_at(run_index))
                       if fn != MODULE_FN and fn not in listed]

            run_changes: dict[str, FnChange] = {}
            for fn in listed:
                after = timeline.at(fn, run_index)
                if after:
                    # The state before this run is the state this one replaced.
                    run_changes[fn] = FnChange(fn, timeline.at(fn, run_index - 1), after)

            run_srcs: dict[str, LineMap] = {}
            if dumped is not None and mapped and dumped.metadata:
                table = parse_debug_table(dumped.metadata)
                entity = canonical_fn(dumped.function)
                if table and entity in run_changes:
                    run_srcs[entity] = map_lines(run_changes[entity].after, table)

            # A map describes a state, not a run, so carry one forward only while
            # the text it was built on still stands — a pass that skipped the
            # function still sees whatever the passes between moved it to.
            for fn, change in run_changes.items():
                if fn in run_srcs:
                    prev_srcs[fn] = (change.after, run_srcs[fn])
                    continue
                held = prev_srcs.get(fn)
                if held is not None and held[0] == change.after:
                    run_srcs[fn] = held[1]
                else:
                    prev_srcs.pop(fn, None)

            run_dots: dict[str, tuple[str | None, str | None]] = {}
            for fn, change in run_changes.items():
                if fn == MODULE_FN:
                    continue  # the module row is a whole-module diff, not a CFG
                run_dots[fn] = (
                    prev_dots.get(fn),
                    ir_cfg_dot(change.after, fn) if change.changed else prev_dots.get(fn),
                )
                prev_dots[fn] = run_dots[fn][1]

            if dumped is not None:
                # Extend after the fill so a multi-function SCC is not listed twice.
                known_fns.extend(
                    fn for fn in split_module_functions(dumped.ir) if fn not in known_fns
                )

            changed = changed or any(c.changed for c in run_changes.values())
            if dumped is not None:
                segments.append(RunSegment(run_index, run_changes, run_dots, run_srcs))
            # The card's own are the last run's, so each of the three agrees.
            fn_changes, dots, src_maps = run_changes, run_dots, run_srcs

        analysed: dict[str, list[str]] = {"run": [], "cached": [], "invalidated": []}
        for state in slot.runs:
            for key, values in analyses.get(state.run.index, {}).items():
                analysed.setdefault(key, []).extend(values)

        last = slot.runs[-1]
        end_line = last.dumps[-1].line if last.dumps else last.run.line
        passes.append(ReportPass(
            id=0,  # assigned by build_report
            lane="ir",
            name=slot.name,
            pass_id=slot.name,
            run_index=slot.run_index,
            changed=changed,
            functions=fn_changes,
            dots=dots,
            runs=segments,
            analyses=analysed,
            log=_pass_log(stderr, slot.run.line, end_line),
            time_ms=time_ms,
            is_custom=_is_custom(slot.name, custom_passes),
            src_maps=src_maps,
            scope=scope_of(slot.run.function),
        ))
    return passes, timeline


def build_lane_a(
    stderr: str,
    custom_passes: tuple[str, ...] = (),
    input_ir: str | None = None,
    mapped: bool = True,
) -> list[ReportPass]:
    """Assemble Lane A (opt) passes from captured stderr."""
    return lane_a_passes(stderr, custom_passes, input_ir, mapped)[0]


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


def build_output_pass(passes: list[ReportPass]) -> ReportPass | None:
    """The synthetic card holding each function's machine IR after the last llc pass."""
    machine = [p for p in passes if not p.is_input and not p.is_output]
    if not machine:
        return None
    functions: dict[str, FnChange] = {}
    dots: dict[str, tuple[str | None, str | None]] = {}
    src_maps: dict[str, Any] = {}
    spills: dict[str, int] = {}
    spill_sites: dict[str, list[dict[str, str]]] = {}
    for pass_ in machine:
        for fn, change in pass_.functions.items():
            functions.pop(fn, None)
            functions[fn] = FnChange(fn, change.after, change.after)
            dots[fn] = (None, pass_.dots.get(fn, (None, None))[1])
            if fn in pass_.src_maps:
                src_maps[fn] = pass_.src_maps[fn]
            else:
                src_maps.pop(fn, None)
            spills[fn] = pass_.spills.get(fn, 0)
            if fn in pass_.spill_sites:
                spill_sites[fn] = pass_.spill_sites[fn]
            else:
                spill_sites.pop(fn, None)
    last = machine[-1]
    return ReportPass(
        id=0,  # assigned by build_report
        lane="mir",
        name=OUTPUT_PASS_NAME,
        pass_id=None,
        run_index=last.run_index + 1,
        time_ms=None,
        changed=True,
        functions=functions,
        dots=dots,
        analyses={"run": []},
        log=f"Machine IR as it left the backend, after every llc pass (last: {last.name}).",
        spills=spills,
        spill_sites=spill_sites,
        asm=next((p.asm for p in reversed(machine) if p.asm), None),
        asm_map=next((p.asm_map for p in reversed(machine) if p.asm), {}),
        src_maps=src_maps,
        is_output=True,
    )


def _node(name: str, kind: str, *, depth: int = 0) -> dict[str, object]:
    return {"name": name, "kind": kind, "depth": depth, "passId": None, "children": []}


def _build_ir_tree(passes: list[ReportPass]) -> dict[str, object]:
    """Synthesize the new-PM pass-manager tree from each pass's scope.

    One leaf per card, a pass that ran again included: the tree is a way into
    the cards, and a card it leaves out is a card with no way in from here.
    """
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
    return lane_b_passes(stderr, asm_text, custom_passes, mir_table)[:3]


def lane_b_passes(
    stderr: str,
    asm_text: str | None = None,
    custom_passes: tuple[str, ...] = (),
    mir_table: DebugTable | None = None,
) -> tuple[list[ReportPass], list[PassNode], str | None, Timeline]:
    """Lane B (llc): its cards, the pass-manager tree, and the MIR timeline."""
    snapshots = parse_mir_snapshots(stderr)
    nodes, pass_arguments = parse_pass_structure(stderr)
    machine_analyses = analyses_by_pass(nodes)
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
        spill_sites: dict[str, list[dict[str, str]]] = {}
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
            if machine_function.spills:
                spill_sites[fn] = [
                    {"kind": s.kind, "slot": s.slot, "block": s.block, "text": s.text}
                    for s in machine_function.spills
                ]
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
            spill_sites=spill_sites,
            log=_mir_log(stderr, first_line, end_line),
            time_ms=summary_ms.get(group[0].pass_name, anchor_ms.get(run_index - 1)),
            is_custom=_is_custom(group[0].pass_name, custom_passes)
                      or _is_custom(pass_id, custom_passes),
            analyses={"run": machine_analyses.get(group[0].pass_name, [])},
            src_maps=src_maps,
        ))

    _attach_reg_maps(passes, order, by_id, fn_seq)
    if passes and order:
        _attach_isel(passes[0], by_id[order[0]], parse_ir_dumps(stderr))
    if passes and asm_text:
        passes[-1].asm = asm_text
        _attach_asm_map(passes[-1], asm_text)
    card_of = {pass_id: (index, by_id[pass_id][0].pass_name)
               for index, pass_id in enumerate(order, start=1)}
    return passes, nodes, pass_arguments, mir_timeline(passes)


def _attach_asm_map(card: ReportPass, asm_text: str) -> None:
    for fn, change in card.functions.items():
        correlation = correlate_asm(change.after, asm_text, fn)
        if correlation:
            card.asm_map[fn] = correlation


def _attach_isel(card: ReportPass, group: list[Any], ir_dumps: list[IrDump]) -> None:
    for snapshot in group:
        fn = next(iter(snapshot.functions))
        change = card.functions.get(fn)
        ir_text = _pre_isel_ir(ir_dumps, fn, snapshot.line)
        if change is None or ir_text is None:
            continue
        correlation = correlate(ir_text, change.after)
        if correlation:
            card.isel_map[fn] = correlation


def _pre_isel_ir(ir_dumps: list[IrDump], fn: str, line: int) -> str | None:
    for dump in reversed(ir_dumps):
        if dump.line >= line:
            continue
        body = split_module_functions(dump.text).get(fn)
        if body:
            return body
    return None


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
    every: list[LineMap] = []
    for pass_ in passes:
        every += list(pass_.src_maps.values())
        for run in pass_.runs:
            every += list(run.src_maps.values())
    if not every:
        return []
    texts = read_sources(every)
    files = sorted(texts)

    def against_files(maps: dict[str, LineMap]) -> dict[str, LineMap]:
        return {fn: encode(mapping, files) for fn, mapping in maps.items()}

    for pass_ in passes:
        pass_.src_maps = against_files(pass_.src_maps)
        for run in pass_.runs:
            run.src_maps = against_files(run.src_maps)
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
    ai_config: dict[str, Any] | None = None,
    clang_args: tuple[str, ...] = (),
    opt_args: tuple[str, ...] = (),
    llc_args: tuple[str, ...] = (),
    target: str | None = None,
    ui: Ui | None = None,
    config_file: str | None = None,
) -> dict[str, Any]:
    """Run the full pipeline and emit the report. Returns a summary dict."""
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    toolchain: Toolchain = discover_toolchain(bin_dir, llvm_version)

    effective_passes = _effective_pipeline(passes, custom_passes)
    # The triple reaches every stage, or the lanes would disagree about the
    # target. clang's driver spells it -target <triple>; the GCC-style
    # "--target <triple>" is rejected outright. Cross-compiling still needs a
    # sysroot for that triple, which is the user's to provide.
    clang_extra = (*clang_args, *(("-target", target) if target else ()))
    llc_extra = (*llc_args, *(("-mtriple", target) if target else ()))

    started = time.perf_counter()
    compiled = compile_to_ir(
        source, toolchain=toolchain, out_dir=raw, timeout=timeout,
        extra_args=clang_extra,
    )
    opt_result = run_opt(
        toolchain, compiled.ir_path, effective_passes,
        out_dir=raw,
        load_pass_plugins=load_pass_plugins,
        print_after=custom_passes,
        extra_args=opt_args,
        timeout=timeout,
    )
    opt_stderr = opt_result.stderr_path.read_text(errors="replace")
    input_ir = compiled.ir_path.read_text(errors="replace")

    input_card = build_input_pass(input_ir, compiled.ir_path, mapped=source_map)
    lane_a = [input_card]

    mir_nodes: list[PassNode] = []
    pass_arguments: str | None = None
    ir_timeline = Timeline()
    if not opt_result.timed_out:
        ir_passes, ir_timeline = lane_a_passes(
            opt_stderr, custom_passes,
            input_ir=input_card.functions[MODULE_FN].after,
            mapped=source_map,
        )
        lane_a += ir_passes

    lane_b: list[ReportPass] = []
    mir_timeline_states = Timeline()
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
            print_after=custom_passes, extra_args=llc_extra,
            timeout=timeout, toolchain=toolchain,
        )
        if not llc_result.timed_out:
            llc_stderr = llc_result.stderr_path.read_text(errors="replace")
            asm_text = llc_result.asm_path.read_text(errors="replace") if llc_result.asm_path else None
            mir_table = harvest_mir_table(
                opt_result.ir_path, toolchain=toolchain,
                load=load, timeout=timeout, extra_args=llc_extra,
            ) if source_map else None
            built, mir_nodes, pass_arguments, mir_timeline_states = lane_b_passes(
                llc_stderr, asm_text, custom_passes, mir_table,
            )
            lane_b += built
            output_card = build_output_pass(built)
            if output_card is not None:
                lane_b.append(output_card)
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
        "mtriple": target,
        "configFile": config_file,
        "ui": (ui or Ui()).as_metadata(),
        "commands": build_commands(compiled, opt_result, llc_result),
        "toolVersions": {name: tool.version for name, tool in toolchain.tools.items()},
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "totalTimeMs": round(total_ms, 1),
        "optCrashed": opt_result.failed,
        "llcCrashed": bool(llc_result and llc_result.failed),
        "finalCfg": final_cfg,
        "analyses": compute_analyses(input_ir),
        "sourceFiles": source_files,
        "pipelineTree": pipeline_tree,
        "passArguments": pass_arguments,
        "errors": {},
    }
    if opt_result.failed:
        metadata["errors"]["opt"] = _tail(opt_stderr)
    if llc_result and llc_result.failed:
        metadata["errors"]["llc"] = _tail(llc_result.stderr_path.read_text(errors="replace"))

    resolved_ai = load_config() if ai_config is None else ai_config
    manifest = emit_report(
        out, passes=all_passes, metadata=metadata,
        frontend_dir=default_frontend_dir(),
        ai_config=resolved_ai,
        blame={
            "ir": blame_document("ir", ir_timeline),
            "mir": blame_document("mir", mir_timeline_states),
        },
    )

    return {
        "reportDir": str(out.resolve()),
        "manifest": str(manifest),
        "laneACount": len(lane_a),
        "laneBCount": len(lane_b),
        "totalTimeMs": round(total_ms, 1),
        "optCrashed": opt_result.failed,
        "llcCrashed": bool(llc_result and llc_result.failed),
        # True when the ask-AI credentials landed in the report directory.
        "aiEmbedded": bool((resolved_ai or {}).get("api_key")),
    }
