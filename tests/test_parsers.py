"""Parser tests against real captured opt/llc output (tests/fixtures/)."""

from __future__ import annotations

from pathlib import Path

from cli.parsers.debug_pass_manager import parse_pass_runs
from cli.parsers.legacy_pass_structure import parse_pass_structure
from cli.parsers.mir import parse_mir_snapshots, vreg_to_physreg
from cli.parsers.print_changed import parse_changed_ir
from cli.parsers.time_passes import parse_time_passes

FIXTURES = Path(__file__).parent / "fixtures"
OPT_STDERR = (FIXTURES / "opt-sample.stderr").read_text()
LLC_STDERR = (FIXTURES / "llc-sample.stderr").read_text()
LLC_CARRY = (FIXTURES / "llc-carry.stderr").read_text()


# --- print_changed ----------------------------------------------------------


def test_parse_changed_ir_headers():
    snapshots = parse_changed_ir(OPT_STDERR)
    assert snapshots
    assert snapshots[0].pass_name == "SimplifyCFGPass"
    assert snapshots[0].function == "main"
    assert "define" in snapshots[0].ir
    pass_names = {s.pass_name for s in snapshots}
    functions = {s.function for s in snapshots}
    assert "SROAPass" in pass_names
    assert {"main", "square"} <= functions


def test_parse_changed_ir_adjacent_snapshots_do_not_leak():
    snapshots = parse_changed_ir(OPT_STDERR)
    for first, second in zip(snapshots, snapshots[1:]):
        assert second.ir != first.ir  # bodies are split at headers
    # first snapshot's body must not contain the second header
    assert "*** IR Dump After" not in snapshots[0].ir


def test_parse_changed_ir_function_dump_stops_at_closing_brace():
    # opt interleaves -debug-pass-manager output right after the function's
    # closing brace; it must not leak into the snapshot (CFG/diff content).
    stderr = (
        "*** IR Dump After LICMPass on flush_cache ***\n"
        "define ptr @flush_cache() {\n"
        "%1:\n"
        "  %.04.lcssa = phi ptr [ %.04, %1 ]\n"
        "  ret ptr %.04.lcssa\n"
        "}\n"
        "Running analysis: MemorySSAAnalysis on flush_cache\n"
        "Running pass: LoopInstSimplifyPass on loop %<unnamed loop> in function flush_cache\n"
        "*** IR Dump After SimplifyCFGPass on flush_cache ***\n"
        "define ptr @flush_cache() {\n"
        "  ret ptr null\n"
        "}\n"
    )
    snapshots = parse_changed_ir(stderr)
    assert [s.pass_name for s in snapshots] == ["LICMPass", "SimplifyCFGPass"]
    assert "Running" not in snapshots[0].ir
    assert snapshots[0].ir.rstrip().endswith("}")


def test_parse_changed_ir_module_dump_keeps_body_but_drops_noise():
    # Module and loop dumps have no closing brace; the body runs to the next
    # header but pass-manager log lines are still filtered out.
    stderr = (
        "*** IR Dump After GlobalOptPass on [module] ***\n"
        "define i32 @a() {\n"
        "  ret i32 1\n"
        "}\n"
        "define i32 @b() {\n"
        "  ret i32 2\n"
        "}\n"
        "Running pass: InstCombinePass on a\n"
        "Invalidating analysis: DominatorTreeAnalysis on a\n"
    )
    snapshots = parse_changed_ir(stderr)
    assert len(snapshots) == 1
    assert "Running" not in snapshots[0].ir
    assert "Invalidating" not in snapshots[0].ir
    assert "@b" in snapshots[0].ir


# --- debug_pass_manager ------------------------------------------------------


def test_parse_pass_runs_fixture():
    runs = parse_pass_runs(OPT_STDERR)
    assert runs
    assert runs[0].name == "MemProfRemoveInfo"
    assert runs[0].function == "[module]"
    names = [r.name for r in runs]
    assert "PromotePass" in names
    assert "SimplifyCFGPass" in names
    assert [r.index for r in runs] == list(range(1, len(runs) + 1))


def test_parse_pass_runs_analysis_attribution():
    stderr = (
        "Running pass: PassA on main\n"
        "Running analysis: DomTree on main\n"
        "Running pass: PassB on main\n"
        "Running analysis: DomTree on main (cached)\n"
        "Invalidating analysis: DomTree on main\n"
    )
    runs = parse_pass_runs(stderr)
    assert [r.name for r in runs] == ["PassA", "PassB"]
    assert [e.name for e in runs[0].analyses] == ["DomTree"]
    assert [(e.name, e.function, e.cached) for e in runs[1].analyses] == [("DomTree", "main", True)]
    assert [e.name for e in runs[1].invalidated] == ["DomTree"]


def test_parse_pass_runs_has_analyses():
    runs = parse_pass_runs(OPT_STDERR)
    assert any(r.analyses for r in runs)
    assert any(r.invalidated for r in runs)


# --- time_passes --------------------------------------------------------------


def test_parse_time_passes_blocks():
    blocks = parse_time_passes(OPT_STDERR)
    assert blocks
    assert all(b.total_seconds > 0 for b in blocks)
    assert any(not b.is_summary for b in blocks)
    assert any(b.is_summary for b in blocks)
    summary = next(b for b in blocks if b.is_summary)
    assert summary.rows  # the big per-analysis table


# --- legacy_pass_structure ------------------------------------------------------


def test_parse_pass_structure_fixture():
    nodes, pass_arguments = parse_pass_structure(LLC_STDERR)
    assert nodes
    assert pass_arguments and "-x86-isel" in pass_arguments
    by_name = {n.name: n for n in nodes}
    assert "ModulePass Manager" in by_name
    assert by_name["ModulePass Manager"].depth == 1
    assert by_name["Pre-ISel Intrinsic Lowering"].depth == 2
    # structure tree uses display names, e.g. "X86 DAG->DAG Instruction Selection"
    assert "X86 DAG->DAG Instruction Selection" in by_name


# --- mir -----------------------------------------------------------------------


def test_parse_mir_snapshots_sample():
    snapshots = parse_mir_snapshots(LLC_STDERR)
    assert snapshots
    pass_ids = [s.pass_id for s in snapshots]
    for expected in ("x86-isel", "greedy", "virtregrewriter", "x86-asm-printer"):
        assert expected in pass_ids, expected
    # llc runs function-at-a-time: each section dumps ONE (pass, function)
    isel = next(s for s in snapshots if s.pass_id == "x86-isel")
    assert len(isel.functions) == 1
    assert "main" in isel.functions
    function = isel.functions["main"]
    assert function.blocks
    assert function.blocks[0].name.startswith("bb.")
    assert function.vregs
    assert function.physregs  # $edi etc. in Live Ins
    # all functions appear somewhere across the dump sequence
    assert {"main", "square"} <= {f for s in snapshots for f in s.functions}


def test_parse_mir_spills_carried_values():
    snapshots = parse_mir_snapshots(LLC_CARRY)
    spill_passes = [s for s in snapshots if any(f.spill_count for f in s.functions.values())]
    assert spill_passes, "expected spill candidates in carry fixture"
    kinds = {kind for s in spill_passes for f in s.functions.values() for kind, _ in f.spills}
    assert "spill" in kinds
    assert "reload" in kinds


def test_vreg_to_physreg_around_rewriter():
    snapshots = parse_mir_snapshots(LLC_CARRY)
    rewriter_index = next(
        i for i, s in enumerate(snapshots) if s.pass_id == "virtregrewriter"
    )
    pre = snapshots[rewriter_index - 1]
    post = snapshots[rewriter_index]
    fn = next(f for f in post.functions if f in pre.functions)
    mapping = vreg_to_physreg(pre.functions[fn], post.functions[fn])
    assert mapping
    values = set(mapping.values())
    assert any(v.startswith("$") for v in values)  # physreg replacements
