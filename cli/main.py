"""CLI entry point: compile -> opt lane -> llc lane -> emit HTML report.

Pipeline (Lane A = opt middle-end, Lane B = llc backend):

    source (.c/.cpp/.ll/.bc)
      -> compile_to_ir (clang -S -emit-llvm -O0, no optnone)
      -> run_opt  -passes=...  (-print-changed=quiet, -debug-pass-manager,
                                -time-passes)            -> Lane A passes
                                (led by build_input_pass: the module before
                                 any pass ran, at run_index 0)
      -> run_llc  (-print-after-all, -debug-pass=Structure, -time-passes)
                                                         -> Lane B passes
      -> emit_report (manifest.json + pass-<id>.json + frontend copy)

A crashed or timed-out opt/llc still produces a partial report: whatever
passes were captured before the failure plus the stderr tail as the error.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import shlex
import sys
import time
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

# opt names a whole-module dump "[module]"; the input card uses the same key
# so the function list reads the same there as on any module pass.
MODULE_FN = "[module]"
# Each lane opens on a synthetic card holding the LLVM IR it was handed:
# lane A gets clang's output, lane B gets that same module after the whole opt
# pipeline has run on it. Neither is a pass.
INPUT_PASS_NAME = "Input IR"
BACKEND_INPUT_PASS_NAME = "Optimized IR"
# A CGSCC pass names its dump after the SCC -- "(main)" -- and a loop pass
# after the loop -- "loop %x in function f" -- neither of which is an entity
# the report tracks. A single-function SCC *is* that function, and a loop's
# changes are changes to the function it sits in, so both join that function's
# own track rather than starting a second one under an alias. Since every dump
# now carries the whole module (-print-module-scope), the folded card shows a
# real before/after of the function instead of a fragment with nothing to diff
# against. A real multi-function SCC ("(a, b)") stays its own entity.
SCC_FN_RE = re.compile(r"^\(([^(),]+)\)$")
LOOP_FN_RE = re.compile(r"^loop .* in function (.+)$")

# Passes opt adds for its own driver duties (input verification, and the
# module printer behind -S). clang's pipeline never runs them, so they are
# omitted from the report to keep lane A comparable to -fdebug-pass-structure.
OPT_DRIVER_PASSES = frozenset({"VerifierPass", "PrintModulePass"})


# --- lane builders -----------------------------------------------------------


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


def _normalize_pass_name(name: str) -> str:
    """Lowercase and strip non-alphanumerics, so a pass class name ("MBAAdd")
    matches its -passes alias ("mba-add") for --custom-pass badging."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _is_custom(name: str | None, custom_passes: tuple[str, ...]) -> bool:
    """True when *name* was declared via --custom-pass.

    Matches case- and punctuation-insensitively: a pass whose class name is
    "MBAAdd" still badges when the user passes its pipeline alias "mba-add".
    """
    if not name or not custom_passes:
        return False
    key = _normalize_pass_name(name)
    return any(key and key == _normalize_pass_name(c) for c in custom_passes)


def _effective_pipeline(passes: str, custom_passes: tuple[str, ...]) -> str:
    """Append --custom-pass names as function passes, deduped, order preserved.

    A plugin function-pass name is rejected at module scope (once a module pass
    like ``default<O2>`` precedes it), so each name is wrapped as
    ``function(<name>)`` — valid at any pipeline position.
    """
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
    """Total seconds of each non-summary time block, attributed to the pass
    whose anchor line precedes it. Returns {anchor_index: ms}."""
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
    """The part of one dump body that belongs to *entity*.

    Every dump arrives at module scope, so a function's card would otherwise
    show the whole module. "[module]" keeps it whole; a function (or a loop
    folded onto the function containing it) takes its own definition out of
    it; a multi-function SCC takes its members, in the order its header names
    them. A body that is not a module -- a bare function dump from a report
    built before -print-module-scope, a loop fragment -- has nothing to carve
    out and is used as it stands.
    """
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
    """Assemble Lane A (opt) passes from the captured stderr.

    *input_ir* is the module as it entered the pipeline (already stripped).
    It seeds the "previous state" of the module and of every function in it,
    so the first pass to touch something diffs against what it actually
    received instead of against nothing.

    *mapped* correlates each snapshot with the source it came from. Every dump
    is printed at module scope and so defines the ``!N`` nodes it references,
    which is why this needs no second opt run: the table is read off the same
    text the snapshot was cut from and cannot describe a different one.
    """
    from .parsers.debug_pass_manager import scope_of

    runs = parse_pass_runs(stderr)
    dumps = parse_changed_ir(stderr)
    time_blocks = parse_time_passes(stderr)

    order: list[str] = []
    first_run_lines: list[int] = []  # aligned with `order`
    # Pass name -> pass-manager scope. The first run's scope wins (a pass that
    # runs at multiple scopes collapses to one card in this lane).
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
    # Last known text of each dumped entity, keyed the way opt names it:
    # "[module]" for a module dump, the bare name for a function dump. Loop
    # and CGSCC dumps ("loop %x in function f", "(f)") are their own keys and
    # have no earlier state to pair with, so they keep an empty "before".
    prev_text: dict[str, str] = {}
    prev_dots: dict[str, str | None] = {}
    # Every function the module holds right now, in module order. opt only
    # dumps what a pass changed, so this is what lets a card also list the
    # functions the pass left alone (see the fill below).
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
        # A dump carries the new state of every function inside it, not just
        # of the entity it is named after: a module pass dumps the whole
        # module, an SCC pass its members. Without this, a function's "before"
        # would skip whatever those passes did to it and blame the next
        # function-scope pass for their changes. On a plain function dump the
        # split returns that same function, so this is simply its own update.
        bodies: dict[str, str] = {}
        for fn, change in fn_changes.items():
            prev_text[fn] = change.after
            bodies.update(split_module_functions(change.after))
        prev_text.update(bodies)
        # Keep cards for passes with no dumps too: -print-changed=quiet only
        # emits IR for what changed, but a pass card with an empty diff still
        # documents that the pass ran (mirrors clang's pass-structure view).
        # The frontend's "changed only" filter hides these by default.
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

        # A module dump is authoritative about what the module holds -- whatever
        # the pass added or deleted included -- so it refreshes the set before
        # this card is filled in.
        if MODULE_FN in fn_changes:
            known_fns = list(split_module_functions(fn_changes[MODULE_FN].after))

        # Every function gets a row on every card, not just the ones this pass
        # dumped: the function list is how you pick a CFG to look at, and a
        # function this pass left alone still has one worth seeing. The filled
        # rows carry the function as it stands after this pass, with
        # changed=False -- so the list dims them, and the "only changed" filter
        # and the per-pass line counts are untouched. Lane B reads this way
        # already, because llc's -print-after-all dumps every function for
        # every pass.
        for fn in known_fns:
            text = prev_text.get(fn)
            if fn in fn_changes or not text:
                continue
            fn_changes[fn] = FnChange(fn, text, text)
            dots[fn] = (prev_dots.get(fn), prev_dots.get(fn))
        # One order for every card, whatever each pass happened to dump: the
        # module, then its functions in module order, then the loop and SCC
        # entities, which are named after the function they sit in.
        ordered = [MODULE_FN] + known_fns + sorted(set(fn_changes) - {MODULE_FN} - set(known_fns))
        fn_changes = {fn: fn_changes[fn] for fn in ordered if fn in fn_changes}

        # A function, loop or SCC dump only ever reveals functions, and this
        # card is already about them -- extending after the fill keeps a
        # multi-function SCC from being listed twice on its own card, once as
        # the SCC entity and once as its members.
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
    return passes, scope_by_name


def build_input_pass(
    ir_text: str,
    origin: Path,
    *,
    lane: str = "ir",
    name: str = INPUT_PASS_NAME,
    note: str = "before any pass ran",
    mapped: bool = True,
) -> ReportPass:
    """The card holding the LLVM IR a lane was handed.

    Lane A gets clang's module; lane B gets that module after the whole opt
    pipeline, which is literally the text llc reads. Neither is a pass, but
    both wear the same shape so the IR and Source views work on them
    unchanged, and both sort at run_index 0, ahead of a pipeline that starts
    at 1. "before" is empty because nothing in that lane precedes them, which
    is also why the viewer withholds the Diff and CFG views (`is_input`) --
    there is nothing to diff against, and the snapshot is a whole module
    rather than one function.

    Unlike a -print-changed dump these modules are self-describing, so their
    own metadata resolves the source mapping with no harvest. The table has
    to be read *before* stripping, which is what removes it.
    """
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


def _build_ir_tree(
    passes: list[ReportPass],
    scope_by_name: dict[str, str],
) -> dict[str, object]:
    """Build the opt (new-PM) pass-manager tree from each pass's inferred scope.

    The new pass manager has no nested structure dump, so the tree is synthesized
    from each pass's scope: module, CGSCC, function and loop passes each group
    under their own collapsible section. The synthetic input card is skipped.
    Loop passes form a flat section (the scope string carries no reliable parent
    function across LLVM versions), not a per-function nesting.
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
        scope = scope_by_name.get(pass_.name, "function")
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
    """Assemble Lane B (llc) passes from the captured stderr.

    llc's machine pass manager runs function-at-a-time: each ``# *** IR Dump
    After <Pass> (id) ***:`` header is followed by exactly one function's
    machine code, and the whole sequence repeats per function. Snapshots are
    therefore regrouped by pass id (first-seen order = pipeline order), with
    each card aggregating the per-function snapshots of that pass.

    Returns ``(passes, structure_nodes, pass_arguments)``: the built cards, the
    raw ``-debug-pass=Structure`` node list (for the hierarchical tree view), and
    the pass-arguments string. The tree itself is built later in
    ``build_report`` once the pass ids have been assigned.
    """
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
    """vreg -> physreg maps from the snapshots around VirtRegRewriter.

    ``pre`` is the last snapshot before the rewriter that carries instructions
    for the function; ``post`` is the rewriter's own snapshot when it has
    content (the usual case), else the first contentful snapshot after it.
    """
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


# What each stage's command line is called in the report's command sheet, and
# what it did. Keyed by the CompiledSource.kind / lane the command belongs to.
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
    """The exact argv of every stage that ran, in run order.

    Reports get read away from the machine that produced them, and the flags
    matter: which clang, which pipeline string, which triple, which plugin .so.
    Each entry carries the argv as a list *and* shell-quoted as one line, so
    the viewer can show it and a reader can paste it to reproduce the stage
    outside the tool. The instrumentation flags are part of the command as run
    and are not filtered out -- the point is exactness, not a tidy retelling.

    A stage that did not run (llc after an opt that emitted nothing) has no
    entry; the sheet then documents exactly how far the pipeline got.
    """
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
    """Read every mapped source file and re-encode the maps against its index.

    Returns the report's ``sourceFiles`` list; passes whose files could not be
    read (a header outside the tree, a moved source) lose their mapping rather
    than pointing at a file the viewer cannot show.
    """
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

    # Custom passes are appended as function passes, force-dumped via
    # -print-after, and badged in the report.
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

    # The input module leads lane A, so the report opens on what clang emitted
    # rather than on the first pass's output. It survives an opt failure.
    input_card = build_input_pass(input_ir, compiled.ir_path, mapped=source_map)
    lane_a = [input_card]

    # Raw structure data for the hierarchical pipeline tree view. Assigned for
    # real below when a lane runs; these defaults cover a timeout/crash/skip.
    ir_scopes: dict[str, str] = {}
    mir_nodes: list[PassNode] = []
    pass_arguments: str | None = None
    if not opt_result.timed_out:
        # Seed from the card's own text, so what the first pass diffs against
        # is byte-for-byte what the input card displays.
        lane_a += build_lane_a(
            opt_stderr, custom_passes,
            input_ir=input_card.functions[MODULE_FN].after,
            mapped=source_map,
        )
        lane_a += lane_a_passes

    lane_b: list[ReportPass] = []
    llc_result = None
    # Lane B is only meaningful when opt actually handed it a module: a failed
    # opt either leaves no final IR or leaves a husk of one, and running llc on
    # that pins opt's failure on the backend (or, for an empty module, produces
    # a full machine lane over no functions that reads as a healthy backend).
    if not opt_result.failed and opt_result.ir_path is not None:
        # Lane B opens on what llc actually reads: the module after every opt
        # pass. Added before llc runs, so it survives a backend crash too.
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
            # llc numbers metadata once for the module it reads and never
            # renumbers, so a single harvested table covers every machine pass.
            mir_table = harvest_mir_table(
                opt_result.ir_path, toolchain=toolchain,
                load=load, timeout=timeout,
            ) if source_map else None
            lane_b += build_lane_b(llc_stderr, asm_text, custom_passes, mir_table)
    total_ms = (time.perf_counter() - started) * 1000.0

    all_passes = lane_a + lane_b
    for index, pass_ in enumerate(all_passes, start=1):
        pass_.id = index

    # Build the hierarchical pass-manager trees now that every pass has an id.
    # Machine-pass leaves link to their ReportPass by name; analyses, print
    # passes and IR-level passes become structural nodes with null pass id.
    mir_by_name = {p.name: p.id for p in lane_b}
    pipeline_tree = {
        "ir": _build_ir_tree(lane_a, ir_scopes),
        "mir": build_tree(mir_nodes, mir_by_name),
    }

    source_files = _attach_source_maps(all_passes)

    # Final state of each function's CFG after the whole pipeline (last card
    # per lane that produced a graph) — shown as the "final CFG" in the UI.
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

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    manifest = emit_report(out, passes=all_passes, metadata=metadata, frontend_dir=frontend_dir)

    return {
        "reportDir": str(out.resolve()),
        "manifest": str(manifest),
        "laneACount": len(lane_a),
        "laneBCount": len(lane_b),
        "totalTimeMs": round(total_ms, 1),
        "optCrashed": opt_result.failed,
        "llcCrashed": bool(llc_result and llc_result.failed),
    }


# --- CLI ----------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llvm-pass-analyzer",
        description=(
            "Analyze LLVM pass pipelines and emit a static HTML report. "
            "Accepts .c/.cpp (compiled with clang), .ll, and .bc sources. "
            "Lane A runs the opt middle-end pipeline; Lane B runs the llc "
            "backend; the report is a browsable HTML report in --output."
        ),
    )
    parser.add_argument("source", help="Source file to analyze (.c/.cpp/.ll/.bc).")
    parser.add_argument("--passes", default=DEFAULT_PASSES,
                        help="New-PM pipeline string for opt (Lane A).  [default: %(default)s]")
    parser.add_argument("--load-pass-plugin", dest="load_pass_plugins",
                        action="append", default=[], metavar="SO",
                        help="New-PM pass plugin .so to load (opt + llc, repeatable).")
    parser.add_argument("--load", dest="load", action="append", default=[],
                        metavar="SO",
                        help="Legacy plugin .so to load (llc backend only, repeatable).")
    parser.add_argument("--custom-pass", dest="custom_passes", action="append",
                        default=[], metavar="NAME",
                        help="Custom function-pass name to append (as function(<name>)) "
                             "and badge (repeatable).")
    parser.add_argument("-o", "--output", default="report",
                        help="Directory to write the report into.  [default: %(default)s]")
    parser.add_argument("--bin-dir", default=None,
                        help="Directory holding the LLVM tools (else LLVM_LENS_BIN_DIR / PATH).")
    parser.add_argument("--llvm-version", type=int, default=None,
                        metavar="MAJOR",
                        help="Expected LLVM major version. Drives the PATH search "
                             "(clang-<MAJOR>) and every tool must report it. The "
                             "parsers target LLVM %(default)s output formats, so "
                             "another major is a porting exercise, not a config "
                             "switch.  [default: %(default)s]")
    parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                        help="Per-tool invocation timeout. Off by default: a big "
                             "module under default<O2> is slow rather than hung, "
                             "and a wall clock that fires mid-pipeline yields a "
                             "partial report that reads like a compiler bug.")
    parser.add_argument("--no-source-map", dest="source_map", action="store_false",
                        default=True,
                        help="Correlate IR/MIR lines with the original source (needs "
                             "debug info; costs one extra opt and llc run).  "
                             "[default: on]")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Analyze LLVM pass pipelines and emit a static HTML report."""
    parser = _parser()
    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_file():
        parser.error(f"argument source: {args.source!r} is not an existing file")

    try:
        summary = build_report(
            source,
            passes=args.passes,
            load_pass_plugins=tuple(args.load_pass_plugins),
            load=tuple(args.load),
            custom_passes=tuple(args.custom_passes),
            output=args.output,
            bin_dir=args.bin_dir,
            llvm_version=args.llvm_version,
            timeout=args.timeout,
            source_map=args.source_map,
        )
    except Exception as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"report:     {summary['reportDir']}/")
    print(f"manifest:   {summary['manifest']}")
    print(f"passes:     {summary['laneACount']} IR, {summary['laneBCount']} machine")
    print(f"total time: {summary['totalTimeMs']:g} ms")
    if summary["optCrashed"]:
        print("warning: opt failed/timed out; report is partial", file=sys.stderr)
    if summary["llcCrashed"]:
        print("warning: llc failed/timed out; report is partial", file=sys.stderr)


if __name__ == "__main__":
    main()
