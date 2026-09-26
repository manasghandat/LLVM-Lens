"""Tests for llvm_lens.api: the IR / machine IR around a named pass."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from llvm_lens import (
    PassSnapshot,
    SnapshotError,
    list_machine_passes,
    machine_ir,
    snapshot,
)
from llvm_lens import api as api_mod
from llvm_lens.compile import CompileError
from llvm_lens.parsers.mir import (
    MachineFunction,
    parse_ir_dumps,
    parse_mir_snapshots,
    split_machine_functions,
)
from llvm_lens.parsers.print_changed import parse_direct_ir
from llvm_lens.runner_llc import llc_command
from tests.conftest import SAMPLE_C

# Two functions' worth of a targeted before-dump, as llc writes it.
BEFORE_MIR = """\
# *** IR Dump Before Greedy Register Allocator (greedy) ***:
# Machine code for function main: IsSSA, TracksLiveness
Function Live Ins: $edi in %0

bb.0 (%ir-block.2):
  successors: %bb.1(0x80000000)
  %1:gr32 = COPY $edi
# End machine code for function main.
# *** IR Dump Before Greedy Register Allocator (greedy) ***:
# Machine code for function sq: IsSSA, TracksLiveness
Function Live Ins: $edi in %0

bb.0 (%ir-block.2):
  successors: %bb.1(0x80000000)
  %1:gr32 = COPY $edi
# End machine code for function sq.
"""

AFTER_MIR = """\
# *** IR Dump After Greedy Register Allocator (greedy) ***:
# Machine code for function main: TracksLiveness
Function Live Ins: $edi in %0

bb.0 (%ir-block.2):
  successors: %bb.1(0x80000000)
  renamable $eax = COPY $edi
  RET64 $eax
# End machine code for function main.
"""


def defines(module_text: str) -> int:
    return sum(1 for line in module_text.splitlines() if line.startswith("define"))


def _mir_parses(toolchain, path) -> bool:
    """True when llc takes *path* as MIR input; it exits non-zero otherwise."""
    proc = subprocess.run(
        [str(toolchain.llc.path), "-x", "mir", "-o", str(path.parent / "out.s"),
         str(path)],
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode == 0


# --- offline: argument validation ---------------------------------------------


def test_snapshot_rejects_an_unknown_direction():
    with pytest.raises(SnapshotError, match="when must be one of"):
        snapshot(SAMPLE_C, "greedy", when="during")


def test_snapshot_rejects_an_unknown_lane():
    with pytest.raises(SnapshotError, match="lane must be one of"):
        snapshot(SAMPLE_C, "greedy", lane="backend")


def test_snapshot_rejects_passes_in_the_machine_lane():
    """An opt pipeline cannot reach a machine pass; say so rather than ignore it."""
    with pytest.raises(SnapshotError, match="passes= selects an opt pipeline"):
        snapshot(SAMPLE_C, "greedy", passes="default<O2>")


def test_snapshot_rejects_an_empty_pipeline():
    with pytest.raises(SnapshotError, match="passes= is empty"):
        snapshot(SAMPLE_C, "instcombine", lane="ir", passes="   ")


def test_snapshot_rejects_a_display_name_before_running_anything():
    """A display name has a space in it, which no pass id does."""
    with pytest.raises(SnapshotError, match="looks like a display name"):
        snapshot(SAMPLE_C, "Greedy Register Allocator", when="after")


def test_snapshot_catches_a_class_name_llc_would_not_reject(toolchain, tmp_path):
    """'InstCombinePass' is not invalid to opt: it just dumps nothing, exiting 0.

    The no-dump check is what turns that into an error rather than no answer.
    """
    with pytest.raises(SnapshotError, match="looks like opt's class name"):
        snapshot(SAMPLE_C, "InstCombinePass", lane="ir", out_dir=tmp_path,
                 toolchain=toolchain)


def test_snapshot_rejects_a_pass_list():
    """-print-* takes a list; answering for two passes at once would be a guess."""
    with pytest.raises(SnapshotError, match="single pass id"):
        snapshot(SAMPLE_C, "greedy,prologepilog")


def test_snapshot_rejects_an_empty_pass_name():
    with pytest.raises(SnapshotError, match="pass_name is empty"):
        snapshot(SAMPLE_C, "  ")


def test_machine_ir_needs_exactly_one_stop_point():
    """llc rejects both together, so neither is as unusable as the pair."""
    with pytest.raises(SnapshotError, match="exactly one of stop_after="):
        machine_ir(SAMPLE_C)
    with pytest.raises(SnapshotError, match="exactly one of stop_after="):
        machine_ir(SAMPLE_C, stop_after="greedy", stop_before="greedy")


def test_machine_ir_rejects_a_display_name_before_running_anything():
    with pytest.raises(SnapshotError, match="looks like a display name"):
        machine_ir(SAMPLE_C, stop_after="Greedy Register Allocator")


def test_list_machine_passes_without_a_source_compiles_a_scratch_file(
    tmp_path, monkeypatch
):
    """Naming no file is a supported call: a scratch C source stands in for one.

    The compile is stubbed, so this pins where the file goes and what is in it
    without needing a toolchain; the end-to-end test below runs it for real.
    """
    seen: dict = {}

    def fake_compile(source, **kwargs):
        seen["source"] = Path(source)
        raise CompileError("stop at the compile: the rest needs a real toolchain")

    monkeypatch.setattr(api_mod, "discover_toolchain", lambda *a, **k: object())
    monkeypatch.setattr(api_mod, "compile_to_ir", fake_compile)

    with pytest.raises(CompileError, match="stop at the compile"):
        list_machine_passes(out_dir=tmp_path)

    source = seen["source"]
    assert source.is_file(), "the scratch file must be written, not just named"
    # A .c: any other suffix is an unsupported input to compile_to_ir.
    assert source.suffix == ".c"
    assert source.parent.parent.resolve() == Path(tempfile.gettempdir()).resolve()
    # out_dir is for captures; the source a caller did not name is not theirs.
    assert source.parent != tmp_path
    assert "int square(int x)" in source.read_text()


# --- offline: the parsers behind the API ---------------------------------------


def test_parse_mir_snapshots_reads_both_directions_and_lowercases_when():
    snapshots = parse_mir_snapshots(BEFORE_MIR + AFTER_MIR)
    assert [s.when for s in snapshots] == ["before", "before", "after"]
    assert [s.pass_id for s in snapshots] == ["greedy", "greedy", "greedy"]
    # Each dump keeps its own header, so `text` is the slice, not a rebuild.
    assert snapshots[0].text == BEFORE_MIR.split("\n# *** IR Dump Before", 1)[0]
    assert snapshots[2].text == AFTER_MIR.rstrip("\n")


def test_split_machine_functions_keeps_what_the_model_drops():
    """The API returns these strings, so a line the model ignores must survive."""
    functions = split_machine_functions(BEFORE_MIR)
    assert list(functions) == ["main", "sq"]
    assert functions["main"] in BEFORE_MIR
    assert functions["main"].startswith("# Machine code for function main: IsSSA")
    assert functions["main"].endswith("# End machine code for function main.")
    assert "successors: %bb.1(0x80000000)" in functions["main"]
    # The model the report uses loses exactly that line.
    model = parse_mir_snapshots(BEFORE_MIR)[0].functions["main"]
    assert isinstance(model, MachineFunction)
    assert "successors:" not in model.text


def test_split_machine_functions_keeps_an_unterminated_dump():
    functions = split_machine_functions(
        "# Machine code for function main: TracksLiveness\nbb.0 (%ir-block.2):\n"
        "  RET64 $eax\n"
    )
    assert functions["main"].endswith("RET64 $eax")


def test_parse_direct_ir_reads_opt_headers():
    """opt writes the header of a named pass commented out; both forms parse."""
    stderr = (
        "; *** IR Dump Before InstCombinePass on sq ***\n"
        "; ModuleID = 'a.c'\n"
        "define i32 @sq(i32 %x) {\n"
        "  %1 = mul i32 %x, %x\n"
        "  ret i32 %1\n"
        "}\n"
        "; *** IR Dump After InstCombinePass on sq ***\n"
        "; ModuleID = 'a.c'\n"
        "define i32 @sq(i32 %x) {\n"
        "  ret i32 %x\n"
        "}\n"
    )
    dumps = parse_direct_ir(stderr)
    assert [(d.when, d.pass_name, d.function) for d in dumps] == [
        ("before", "InstCombinePass", "sq"),
        ("after", "InstCombinePass", "sq"),
    ]
    assert "mul i32" in dumps[0].raw
    assert "mul i32" not in dumps[1].raw


def test_parse_ir_dumps_reads_llc_ir_level_headers():
    """llc dumps its IR passes in opt's old shape, with the id in parentheses."""
    stderr = (
        "*** IR Dump Before CodeGen Prepare (codegenprepare) ***\n"
        "define i32 @main() {\n"
        "  ret i32 0\n"
        "}\n"
    )
    dump = parse_ir_dumps(stderr)[0]
    assert (dump.when, dump.pass_id, dump.pass_name) == (
        "before", "codegenprepare", "CodeGen Prepare"
    )


# --- the lean invocation -------------------------------------------------------


def test_llc_command_targeted_dumps_only_the_named_pass(tmp_path):
    """The targeted mode must not ask for every dump to answer about one."""
    cmd = llc_command(
        Path("/llvm/llc"), Path("in.ll"), out=tmp_path / "out.s",
        print_after=("greedy",), print_all=False,
    )
    assert cmd == [
        "/llvm/llc",
        "-print-after=greedy",
        "-o", str(tmp_path / "out.s"), "in.ll",
    ]


def test_llc_command_targeted_before(tmp_path):
    cmd = llc_command(
        Path("/llvm/llc"), Path("in.ll"), out=tmp_path / "out.s",
        print_before=("x86-isel",), print_all=False,
    )
    assert "-print-before=x86-isel" in cmd
    assert "-print-after-all" not in cmd


# --- end to end ----------------------------------------------------------------


@pytest.fixture(scope="module")
def machine_passes(toolchain, tmp_path_factory):
    """The (id, display name) pairs llc runs on the sample, once for the module."""
    return list_machine_passes(
        SAMPLE_C, out_dir=tmp_path_factory.mktemp("passes"), toolchain=toolchain
    )


def _id_matching(pairs, needle):
    """The id whose display name contains *needle*, or skip."""
    for pass_id, name in pairs:
        if needle in name.lower():
            return pass_id
    pytest.skip(f"this target's pipeline has no machine pass matching {needle!r}")


def test_list_machine_passes_reports_ids_and_names(toolchain, tmp_path):
    pairs = list_machine_passes(SAMPLE_C, out_dir=tmp_path, toolchain=toolchain)
    assert pairs, "expected llc to report the passes it runs"
    ids = [pass_id for pass_id, _ in pairs]
    assert len(ids) == len(set(ids))  # a pass that runs twice is listed once
    for pass_id, name in pairs:
        assert pass_id and " " not in pass_id, (pass_id, name)


def test_list_machine_passes_answers_without_a_source(toolchain, tmp_path):
    """The scratch source must run a real backend pipeline, not an empty one."""
    pairs = list_machine_passes(out_dir=tmp_path, toolchain=toolchain)
    assert pairs, "expected llc to report the passes it runs"
    ids = [pass_id for pass_id, _ in pairs]
    assert len(ids) == len(set(ids))
    for pass_id, name in pairs:
        assert pass_id and " " not in pass_id, (pass_id, name)
    assert not list(tmp_path.glob("*.c")), "the scratch source belongs in temp"


def test_machine_ir_is_the_serialization_format_not_a_dump(toolchain, tmp_path):
    """YAML documents, not llc's `# Machine code for function` print output."""
    mir = machine_ir(SAMPLE_C, stop_after="x86-isel", out_dir=tmp_path,
                     toolchain=toolchain)
    assert mir.startswith("--- |"), "the first document is the module's IR"
    assert "# Machine code for function" not in mir
    # One bare `---` per machine function, after that first, flagged document.
    markers = [line for line in mir.splitlines() if line == "---"]
    names = [line.split(":", 1)[1].strip() for line in mir.splitlines()
             if line.startswith("name:")]
    assert names, "expected a document per machine function"
    assert len(markers) == len(names)
    assert "main" in names
    # Returned verbatim, and left where the rest of a run's captures go.
    assert (tmp_path / "raw" / "machine.mir").read_text() == mir


def test_machine_ir_round_trips_through_llc_where_a_dump_does_not(
    toolchain, tmp_path
):
    """The point of the lane: LLVM's own MIR parser takes this text, and not a
    -print-after dump."""
    path = tmp_path / "round-trip.mir"
    path.write_text(
        machine_ir(SAMPLE_C, stop_after="x86-isel", out_dir=tmp_path,
                   toolchain=toolchain)
    )
    assert _mir_parses(toolchain, path)

    path.write_text(
        snapshot(SAMPLE_C, "x86-isel", out_dir=tmp_path, toolchain=toolchain).text
    )
    assert not _mir_parses(toolchain, path)


def test_machine_ir_stop_direction_picks_the_side_of_the_pass(toolchain, tmp_path):
    """stop_before is the state the pass saw; stop_after the state it left."""
    before = machine_ir(SAMPLE_C, stop_before="greedy", out_dir=tmp_path,
                        toolchain=toolchain)
    after = machine_ir(SAMPLE_C, stop_after="greedy", out_dir=tmp_path,
                       toolchain=toolchain)
    assert before != after
    for text in (before, after):
        assert text.startswith("--- |")
        assert "name:" in text


def test_machine_ir_simplify_keeps_the_machine_code(toolchain, tmp_path):
    """-simplify-mir leaves out default-valued fields, not the machine code:
    the text is shorter and still MIR."""
    full = machine_ir(SAMPLE_C, stop_after="x86-isel", out_dir=tmp_path,
                      toolchain=toolchain)
    lean = machine_ir(SAMPLE_C, stop_after="x86-isel", simplify=True,
                      out_dir=tmp_path, toolchain=toolchain)
    assert len(lean) < len(full)
    assert "name:" in lean
    path = tmp_path / "lean.mir"
    path.write_text(lean)
    assert _mir_parses(toolchain, path)


def test_machine_ir_without_debug_info_is_the_same_code_unannotated(
    toolchain, tmp_path
):
    """debug_info=False drops the metadata, not the machine code: take the
    annotations off both texts and the instructions match line for line."""
    full = machine_ir(SAMPLE_C, stop_after="x86-isel", out_dir=tmp_path,
                      toolchain=toolchain)
    bare = machine_ir(SAMPLE_C, stop_after="x86-isel", debug_info=False,
                      out_dir=tmp_path, toolchain=toolchain)
    assert bare.startswith("--- |")
    assert "name:" in bare
    assert len(bare) < len(full)
    assert "debug-location" in full and "debug-location" not in bare
    assert "!dbg" in full and "!dbg" not in bare
    # The stack entries keep the keys without -g, but empty: they point at
    # metadata that no longer exists.
    assert "debug-info-variable: '!" in full
    assert "debug-info-variable: ''" in bare
    assert "debug-info-variable: '!" not in bare

    def code(text):
        """Every instruction or IR line, with annotations that are not code off.

        The IR document is indented like an instruction, so it is in here too;
        that is the point — the embedded IR must agree as well. Metadata
        numbering is left out of the comparison: the numbers differ because
        the module without -g has fewer nodes, not because the code does.
        """
        lines = []
        for line in text.splitlines():
            if not re.match(r"^    [A-Za-z$%]", line):
                continue
            if line.lstrip().startswith("debug-"):  # stack entry keys
                continue
            # No comma when the instruction has no operands: `LFENCE debug-...`
            line = re.sub(r",? ?(?:debug-location|!dbg) !\d+", "", line)
            lines.append(re.sub(r"!([\w.]+) !\d+", r"!\1 !N", line))
        return lines

    assert code(bare) == code(full)
    path = tmp_path / "bare.mir"
    path.write_text(bare)
    assert _mir_parses(toolchain, path)


def test_snapshot_machine_unknown_id_raises(toolchain, tmp_path):
    """llc exits 0 with no dump here; the API must not call that an answer."""
    with pytest.raises(SnapshotError, match="no machine pass named 'notapass'"):
        snapshot(SAMPLE_C, "notapass", out_dir=tmp_path, toolchain=toolchain)


def test_snapshot_machine_is_verbatim_and_lean(toolchain, machine_passes, tmp_path):
    isel = _id_matching(machine_passes, "instruction selection")
    for when in ("before", "after"):
        snap = snapshot(SAMPLE_C, isel, when=when, out_dir=tmp_path,
                        toolchain=toolchain)
        assert isinstance(snap, PassSnapshot)
        assert (snap.lane, snap.format, snap.when) == ("machine", "mir", when)
        assert snap.pass_id == isel
        assert snap.functions, f"{when} dump named no function"
        assert snap.occurrence == 0
        # Targeted, not the report's sweep: one pass dumped, not every pass.
        assert f"-print-{when}={isel}" in snap.cmd
        assert "-print-after-all" not in snap.cmd
        # Every entry is llc's own text for that function, end markers and all.
        raw = snap.stderr_path.read_text(errors="replace")
        for name, text in snap.functions.items():
            assert text in raw
            assert text.startswith(f"# Machine code for function {name}: ")
            assert text.endswith(f"# End machine code for function {name}.")
        assert snap.text in raw  # one dump per function, so the run is contiguous


def test_snapshot_machine_direction_selects_the_state(toolchain, machine_passes,
                                                      tmp_path):
    """Selection is the widest gap in the backend: no instructions yet, then all.

    The allocator is a poor probe — on this LLVM its dump looks the same either
    side, the rewriting happening in virtregrewriter.
    """
    isel = _id_matching(machine_passes, "instruction selection")
    before = snapshot(SAMPLE_C, isel, when="before", out_dir=tmp_path,
                      toolchain=toolchain)
    after = snapshot(SAMPLE_C, isel, when="after", out_dir=tmp_path,
                     toolchain=toolchain)
    assert list(before.functions) == list(after.functions)
    for name in before.functions:
        assert len(after.functions[name]) > len(before.functions[name]), name
    assert "MOV" in after.text or "$" in after.text


def test_snapshot_machine_run_is_one_pipeline_position(toolchain, machine_passes,
                                                       tmp_path):
    """A pass listed twice dumps each function twice, one run per position.

    llc's legacy pass manager runs function-at-a-time, so the second greedy on a
    function follows the first immediately. Grouping by adjacency would cut the
    runs across different functions.
    """
    allocator = _id_matching(machine_passes, "greedy")
    snap = snapshot(SAMPLE_C, allocator, when="after", out_dir=tmp_path,
                    toolchain=toolchain)
    assert snap.runs
    expected = set(snap.runs[0].functions)
    assert len(expected) > 1
    for run in snap.runs:
        assert set(run.functions) == expected
        assert run.line > 0
    assert snap.occurrence == len(snap.runs) - 1  # "after" means the last run

    first = snapshot(SAMPLE_C, allocator, when="after", occurrence=0,
                     out_dir=tmp_path, toolchain=toolchain)
    assert first.occurrence == 0
    assert first.text == snap.runs[0].text
    if len(snap.runs) > 1:
        assert snap.runs[0].text != snap.runs[1].text


def test_snapshot_machine_out_of_range_occurrence_raises(toolchain, machine_passes,
                                                         tmp_path):
    allocator = _id_matching(machine_passes, "greedy")
    with pytest.raises(SnapshotError, match="occurrence must be"):
        snapshot(SAMPLE_C, allocator, occurrence=99, out_dir=tmp_path,
                 toolchain=toolchain)


def test_snapshot_ir_lane_before_instcombine(toolchain, tmp_path):
    snap = snapshot(SAMPLE_C, "instcombine", when="before", lane="ir",
                    out_dir=tmp_path, toolchain=toolchain)
    assert (snap.lane, snap.format, snap.pass_id) == ("ir", "ir", "instcombine")
    # The header names the class, so pass_name is opt's spelling, not ours.
    assert snap.pass_name.endswith("Pass")
    assert snap.functions
    for name, text in snap.functions.items():
        assert f"@{name}(" in text
        assert text.rstrip().endswith("}")
    # -print-module-scope: one dump holds the whole module, split here by function.
    assert defines(snap.text) == len(snap.functions)


def test_snapshot_ir_lane_keeps_every_run(toolchain, tmp_path):
    """instcombine runs many times under default<O2>; each state is kept."""
    snap = snapshot(SAMPLE_C, "instcombine", lane="ir", out_dir=tmp_path,
                    toolchain=toolchain)
    assert len(snap.runs) > 1, "expected instcombine to run more than once"
    assert snap.occurrence == len(snap.runs) - 1  # default "after" is the last run
    assert all(run.line > 0 for run in snap.runs)
    assert len({run.text for run in snap.runs}) > 1, "runs should not be identical"

    second = snapshot(SAMPLE_C, "instcombine", lane="ir", occurrence=1,
                      out_dir=tmp_path, toolchain=toolchain)
    assert second.occurrence == 1
    assert second.text == snap.runs[1].text
    assert second.functions["main"] == snap.runs[1].functions["main"]


def test_snapshot_ir_lane_takes_a_pipeline(toolchain, tmp_path):
    snap = snapshot(SAMPLE_C, "mem2reg", lane="ir", passes="mem2reg",
                    out_dir=tmp_path, toolchain=toolchain)
    assert (snap.format, snap.pass_id) == ("ir", "mem2reg")
    assert "define" in snap.functions["main"]


def test_snapshot_ir_lane_unknown_pass_raises(toolchain, tmp_path):
    with pytest.raises(SnapshotError, match="no pass named 'notapass'"):
        snapshot(SAMPLE_C, "notapass", lane="ir", out_dir=tmp_path,
                 toolchain=toolchain)


def test_snapshot_machine_lane_can_still_answer_with_ir(toolchain, machine_passes,
                                                        tmp_path):
    """llc runs IR passes too; naming one returns IR and says so."""
    ir_id = _id_matching(machine_passes, "codegen prepare")
    snap = snapshot(SAMPLE_C, ir_id, out_dir=tmp_path, toolchain=toolchain)
    assert snap.lane == "machine"
    assert snap.format == "ir"
    assert snap.pass_id == ir_id
    assert defines(snap.text) == len(snap.functions)


def test_snapshot_machine_lane_keeps_a_pass_that_names_no_function(
        toolchain, machine_passes, tmp_path):
    """llc's legacy loop passes print a loop, under a header naming no function.

    There is no function to key such a dump by; the state is returned in `text`
    rather than dropped for want of a label.
    """
    loop_id = _id_matching(machine_passes, "loop strength reduction")
    snap = snapshot(SAMPLE_C, loop_id, out_dir=tmp_path, toolchain=toolchain)
    assert snap.format == "ir"
    assert snap.text.strip()
    assert snap.functions == {}
    assert snap.runs and all(run.text.strip() for run in snap.runs)
