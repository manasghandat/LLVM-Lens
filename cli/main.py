"""CLI entry point: compile -> opt lane -> llc lane -> emit HTML report.

Pipeline (Lane A = opt middle-end, Lane B = llc backend):

    source (.c/.cpp/.ll/.bc)
      -> compile_to_ir (clang -S -emit-llvm -O0, no optnone)
      -> run_opt  -passes=...  (-print-changed=quiet, -debug-pass-manager,
                                -time-passes)            -> Lane A passes
      -> run_llc  (-print-after-all, -debug-pass=Structure, -time-passes)
                                                         -> Lane B passes
      -> emit_report (manifest.json + pass-<id>.json + frontend copy)

A crashed or timed-out opt/llc still produces a partial report: whatever
passes were captured before the failure plus the stderr tail as the error.
"""

from __future__ import annotations

import datetime as _dt
import re
import time
from pathlib import Path
from typing import Any

import click

from .cfg import ir_cfg_dot, machine_cfg_dot
from .compile import compile_to_ir
from .diff import FnChange
from .emit import ReportPass, emit_report
from .parsers.debug_pass_manager import parse_pass_runs
from .parsers.legacy_pass_structure import parse_pass_structure
from .parsers.mir import parse_mir_snapshots, vreg_to_physreg
from .parsers.print_changed import parse_changed_ir
from .parsers.time_passes import parse_time_passes
from .runner_llc import run_llc
from .runner_opt import run_opt
from .toolchain import Toolchain, discover_toolchain

IR_DUMP_HEADER_RE = re.compile(r"^\*\*\* IR Dump After ")
MACHINE_HEADER_RE = re.compile(r"^# \*\*\* IR Dump After ")

DEFAULT_PASSES = "default<O2>"

# Passes opt adds for its own driver duties (input verification, and the
# module printer behind -S). clang's pipeline never runs them, so they are
# omitted from the report to keep lane A comparable to -fdebug-pass-structure.
OPT_DRIVER_PASSES = frozenset({"VerifierPass", "PrintModulePass"})


# --- lane builders -----------------------------------------------------------


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join((text or "").splitlines()[-lines:])


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


def build_lane_a(stderr: str) -> list[ReportPass]:
    """Assemble Lane A (opt) passes from the captured stderr."""
    runs = parse_pass_runs(stderr)
    dumps = parse_changed_ir(stderr)
    time_blocks = parse_time_passes(stderr)

    order: list[str] = []
    first_run_lines: list[int] = []  # aligned with `order`
    for run in runs:
        if run.name not in order:
            order.append(run.name)
            first_run_lines.append(run.line)

    snap: dict[tuple[str, str], str] = {}
    fn_order: dict[str, int] = {}
    for dump in dumps:
        snap.setdefault((dump.pass_name, dump.function), dump.ir)
        fn_order.setdefault(dump.function, len(fn_order))

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
    prev_text: dict[str, str] = {}
    prev_dots: dict[str, str | None] = {}
    line_count = len(stderr.splitlines()) + 1
    for run_index, name in enumerate(order, start=1):
        fn_changes: dict[str, FnChange] = {}
        for fn in sorted(fn_order, key=fn_order.get):
            after = snap.get((name, fn))
            if after is None:
                continue
            fn_changes[fn] = FnChange(fn, prev_text.get(fn, ""), after)
        for fn, change in fn_changes.items():
            prev_text[fn] = change.after
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
        ))
    return passes


def build_lane_b(stderr: str, asm_text: str | None = None) -> list[ReportPass]:
    """Assemble Lane B (llc) passes from the captured stderr.

    llc's machine pass manager runs function-at-a-time: each ``# *** IR Dump
    After <Pass> (id) ***:`` header is followed by exactly one function's
    machine code, and the whole sequence repeats per function. Snapshots are
    therefore regrouped by pass id (first-seen order = pipeline order), with
    each card aggregating the per-function snapshots of that pass.
    """
    snapshots = parse_mir_snapshots(stderr)
    parse_pass_structure(stderr)  # validated by tests; ordering comes from dumps
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
        ))

    _attach_reg_maps(passes, order, by_id, fn_seq)
    if passes and asm_text:
        passes[-1].asm = asm_text
    return passes


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


def build_report(
    source: str | Path,
    *,
    passes: str = DEFAULT_PASSES,
    load_pass_plugins: tuple[str, ...] = (),
    mtriple: str | None = None,
    output: str | Path = "report",
    bin_dir: str | Path | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Run the full pipeline and emit the report. Returns a summary dict."""
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)

    toolchain: Toolchain = discover_toolchain(bin_dir)

    started = time.perf_counter()
    compiled = compile_to_ir(source, out_dir=raw, toolchain=toolchain, timeout=timeout)
    opt_result = run_opt(
        compiled.ir_path, passes,
        out_dir=raw, mtriple=mtriple,
        load_pass_plugins=load_pass_plugins,
        timeout=timeout, toolchain=toolchain,
    )
    opt_stderr = opt_result.stderr_path.read_text(errors="replace")
    lane_a = build_lane_a(opt_stderr) if not opt_result.timed_out else []

    lane_b: list[ReportPass] = []
    llc_result = None
    if opt_result.ir_path is not None:
        llc_result = run_llc(
            opt_result.ir_path, out_dir=raw, mtriple=mtriple,
            timeout=timeout, toolchain=toolchain,
        )
        if not llc_result.timed_out:
            llc_stderr = llc_result.stderr_path.read_text(errors="replace")
            asm_text = llc_result.asm_path.read_text(errors="replace") if llc_result.asm_path else None
            lane_b = build_lane_b(llc_stderr, asm_text)
    total_ms = (time.perf_counter() - started) * 1000.0

    all_passes = lane_a + lane_b
    for index, pass_ in enumerate(all_passes, start=1):
        pass_.id = index

    metadata = {
        "source": str(compiled.source_path),
        "inputKind": compiled.kind,
        "pipeline": passes,
        "mtriple": mtriple,
        "toolVersions": {name: tool.version for name, tool in toolchain.tools.items()},
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "totalTimeMs": round(total_ms, 1),
        "optCrashed": opt_result.failed,
        "llcCrashed": bool(llc_result and llc_result.failed),
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


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.option("--passes", default=DEFAULT_PASSES, show_default=True,
              help="New-PM pipeline string for opt (Lane A).")
@click.option("--load-pass-plugin", "load_pass_plugins", multiple=True,
              help="Pass plugin .so to load (repeatable).")
@click.option("--mtriple", default=None, help="Target triple override, e.g. x86_64.")
@click.option("-o", "--output", default="report", show_default=True,
              help="Directory to write the report into.")
@click.option("--bin-dir", default=None,
              help="Directory holding the LLVM tools (else LLVM_LENS_BIN_DIR / PATH).")
@click.option("--timeout", type=float, default=60.0, show_default=True,
              help="Per-tool invocation timeout in seconds.")
def main(
    source: str,
    passes: str,
    load_pass_plugins: tuple[str, ...],
    mtriple: str | None,
    output: str,
    bin_dir: str | None,
    timeout: float,
) -> None:
    """Analyze LLVM pass pipelines and emit a static HTML report.

    Accepts .c/.cpp (compiled with clang), .ll, and .bc sources. Lane A runs
    the opt middle-end pipeline; Lane B runs the llc backend; the report is a
    browsable HTML report in --output.
    """
    try:
        summary = build_report(
            source,
            passes=passes,
            load_pass_plugins=tuple(load_pass_plugins),
            mtriple=mtriple,
            output=output,
            bin_dir=bin_dir,
            timeout=timeout,
        )
    except (click.ClickException,):
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"report:     {summary['reportDir']}/")
    click.echo(f"manifest:   {summary['manifest']}")
    click.echo(f"passes:     {summary['laneACount']} IR, {summary['laneBCount']} machine")
    click.echo(f"total time: {summary['totalTimeMs']:g} ms")
    if summary["optCrashed"]:
        click.echo("warning: opt failed/timed out; report is partial", err=True)
    if summary["llcCrashed"]:
        click.echo("warning: llc failed/timed out; report is partial", err=True)


if __name__ == "__main__":
    main()
