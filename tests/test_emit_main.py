"""Tests for cli.emit (report emission) and cli.main (lane builders, pipeline)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from cli.diff import FnChange
from cli.emit import ReportPass, emit_report
from cli.main import (
    BACKEND_INPUT_PASS_NAME, INPUT_PASS_NAME, MODULE_FN, _effective_pipeline,
    build_commands, build_input_pass, build_lane_a, build_lane_b, build_report,
)
from cli.parsers.print_changed import strip_module_noise
from cli.sourcemap import SourceRef
from tests.conftest import SAMPLE_C

FRONTEND = Path(__file__).parent.parent / "frontend"


def _passes_fixture() -> list[ReportPass]:
    change = FnChange("main", "before IR", "after IR")
    return [
        ReportPass(
            id=1, lane="ir", name="SROAPass", pass_id="SROAPass",
            run_index=1, time_ms=0.5, changed=True,
            functions={"main": change},
            dots={"main": ("digraph { }", "digraph { }")},
            analyses={"run": ["DomTree on main"], "cached": [], "invalidated": []},
            log="debug line",
        ),
        ReportPass(
            id=2, lane="mir", name="Greedy", pass_id="greedy",
            run_index=1, time_ms=2.0, changed=True,
            functions={"main": change},
            spills={"main": 3},
            reg_map={"main": {"0": "$eax"}},
            asm="main:\n  ret\n",
        ),
    ]


# --- emit ----------------------------------------------------------------------


def test_emit_report_writes_layout(tmp_path):
    manifest = emit_report(
        tmp_path / "report",
        passes=_passes_fixture(),
        metadata={"source": "a.c", "pipeline": "default<O1>"},
        frontend_dir=FRONTEND,
    )
    report = tmp_path / "report"
    assert (report / "index.html").is_file()
    assert (report / "app.js").is_file()
    assert (report / "style.css").is_file()

    data = manifest.parent
    manifest_data = json.loads(manifest.read_text())
    assert manifest_data["metadata"]["pipeline"] == "default<O1>"
    assert [p["id"] for p in manifest_data["passes"]] == [1, 2]
    assert manifest_data["passes"][1]["spillCount"] == 3
    assert manifest_data["passes"][0]["analysisCounts"] == {"run": 1, "cached": 0, "invalidated": 0}

    pass1 = json.loads((data / "pass-1.json").read_text())
    assert pass1["functions"]["main"]["before"] == "before IR"
    assert pass1["functions"]["main"]["dotBefore"] == "digraph { }"
    assert pass1["lane"] == "ir"
    assert "spills" not in pass1

    pass2 = json.loads((data / "pass-2.json").read_text())
    assert pass2["spills"] == {"main": 3}
    assert pass2["regMap"] == {"main": {"0": "$eax"}}
    assert pass2["asm"] == "main:\n  ret\n"

    # file://-safe script wrappers
    assert "__LLVM_LENS_MANIFEST__" in (data / "manifest.js").read_text()
    assert "window.__LLVM_LENS_DATA__" in (data / "pass-1.js").read_text()


def test_emit_report_stamps_assets_so_a_rebuild_is_not_served_from_cache(tmp_path):
    """A rebuilt report must not load the previous build's app.js."""
    report = tmp_path / "report"
    emit_report(report, passes=_passes_fixture(), metadata={}, frontend_dir=FRONTEND)
    first = (report / "index.html").read_text()
    stamps = re.findall(r'(?:href|src)="([^"]+\?v=[0-9a-f]{12})"', first)
    # Every script and stylesheet reference carries a digest.
    assert len(stamps) == len(re.findall(r'(?:href|src)="[^"]+\.(?:js|css)', first))

    # Same assets -> same stamps, so unchanged files stay cacheable.
    emit_report(report, passes=_passes_fixture(), metadata={}, frontend_dir=FRONTEND)
    assert (report / "index.html").read_text() == first

    # A changed asset gets a new stamp, which is what forces the reload.
    (report / "app.js").write_text("/* different */\n")
    from cli.emit import _stamp_assets
    _stamp_assets(report)
    assert re.search(r'src="app\.js\?v=([0-9a-f]{12})"', (report / "index.html").read_text()
                     ).group(1) != re.search(r'src="app\.js\?v=([0-9a-f]{12})"', first).group(1)


def test_emit_report_missing_frontend_is_tolerated(tmp_path):
    emit_report(
        tmp_path / "r", passes=_passes_fixture(),
        metadata={}, frontend_dir=tmp_path / "nope",
    )
    assert (tmp_path / "r" / "data" / "manifest.json").is_file()


def test_manifest_carries_line_deltas_for_both_lanes(tmp_path):
    """llc reports no analyses, so the delta is lane B's only per-pass number."""
    ir = ReportPass(
        id=1, lane="ir", name="SROAPass", pass_id="SROAPass", run_index=1,
        time_ms=None, changed=True,
        functions={"main": FnChange("main", "a\nb\nc\n", "a\nx\n")},
    )
    mir = ReportPass(
        id=2, lane="mir", name="Greedy", pass_id="greedy", run_index=1,
        time_ms=None, changed=True,
        functions={
            "main": FnChange("main", "", "p\nq\n"),
            "getTime": FnChange("getTime", "r\n", "r\n"),
        },
    )
    card = ReportPass(
        id=3, lane="mir", name=BACKEND_INPUT_PASS_NAME, pass_id=None, run_index=0,
        time_ms=None, changed=True, is_input=True,
        functions={MODULE_FN: FnChange(MODULE_FN, "", "whole\nmodule\n")},
    )
    manifest = emit_report(
        tmp_path / "report", passes=[ir, mir, card],
        metadata={}, frontend_dir=FRONTEND,
    )
    deltas = {p["name"]: p["lineDelta"] for p in json.loads(manifest.read_text())["passes"]}
    assert deltas["SROAPass"] == {"added": 1, "removed": 2}
    # Summed across every function the machine pass touched.
    assert deltas["Greedy"] == {"added": 2, "removed": 0}
    # Nothing precedes an input card, so its whole module is not "added".
    assert deltas[BACKEND_INPUT_PASS_NAME] is None


def test_emit_report_serializes_is_custom(tmp_path):
    custom = ReportPass(
        id=9, lane="ir", name="MyCustomPass", pass_id="MyCustomPass",
        run_index=1, time_ms=None, changed=False, is_custom=True,
    )
    manifest = emit_report(
        tmp_path / "report", passes=[custom],
        metadata={"source": "a.c", "pipeline": "default<O2>"},
        frontend_dir=FRONTEND,
    )
    data = manifest.parent
    manifest_data = json.loads(manifest.read_text())
    assert manifest_data["passes"][0]["isCustom"] is True
    assert json.loads((data / "pass-9.json").read_text())["isCustom"] is True


# --- lane builders (fixture-based, no toolchain) --------------------------------


def test_build_lane_a_on_fixture(capture):
    passes = build_lane_a(capture("opt-sample.stderr"))
    assert passes
    assert all(p.lane == "ir" for p in passes)
    names = [p.name for p in passes]
    assert "SROAPass" in names
    assert "SimplifyCFGPass" in names
    # -print-changed=quiet only dumps changed IR, but every pass that ran
    # gets a card; transforming passes are the changed=True ones.
    assert any(p.changed for p in passes)
    assert any(not p.changed for p in passes)
    # run_index is the position in the full pipeline, so it is sequential.
    assert [p.run_index for p in passes] == list(range(1, len(passes) + 1))
    # -print-changed=quiet dumps only the functions a pass changed, so no
    # single card necessarily shows both; every function appears somewhere.
    all_fns = set().union(*(set(p.functions) for p in passes))
    assert {"main", "square"} <= all_fns
    assert any(p.time_ms is not None for p in passes)  # time-passes attribution
    assert any(p.analyses["run"] for p in passes)
    assert any(p.dots.get("main") and p.dots["main"][1] for p in passes)  # DOT generated



def test_build_lane_a_pairs_the_first_dump_against_the_input_module():
    # Without a seed the first pass to touch something diffs against nothing,
    # which reads as "the whole function was added" -- false.
    module = (
        "; Function Attrs: nounwind\n"
        "define i32 @main() {\n"
        "  %1 = add i32 1, 1\n"
        "  ret i32 %1\n"
        "}\n"
    )
    stderr = (
        "Running pass: InstCombinePass on main\n"
        "*** IR Dump After InstCombinePass on main ***\n"
        "; Function Attrs: nounwind\n"
        "define i32 @main() {\n"
        "  ret i32 2\n"
        "}\n"
    )
    unseeded = build_lane_a(stderr)[0].functions["main"]
    assert unseeded.before == ""

    seeded = build_lane_a(stderr, input_ir=module)[0].functions["main"]
    assert seeded.before == module.rstrip("\n")  # the function as it arrived
    assert seeded.changed


def test_build_lane_a_carries_module_pass_changes_into_the_function_track():
    # A module pass dumps the whole module; the functions inside it are the
    # new state of those functions, so the next function-scope dump must not
    # be blamed for what the module pass did.
    stderr = (
        "Running pass: GlobalOptPass on [module]\n"
        "*** IR Dump After GlobalOptPass on [module] ***\n"
        "define i32 @main() {\n"
        "  %1 = add i32 1, 1\n"
        "  ret i32 %1\n"
        "}\n"
        "Running pass: InstCombinePass on main\n"
        "*** IR Dump After InstCombinePass on main ***\n"
        "define i32 @main() {\n"
        "  ret i32 2\n"
        "}\n"
    )
    passes = build_lane_a(stderr, input_ir="define i32 @main() {\n  ret i32 undef\n}")
    instcombine = next(p for p in passes if p.name == "InstCombinePass")
    assert "%1 = add i32 1, 1" in instcombine.functions["main"].before
    assert "undef" not in instcombine.functions["main"].before


def test_build_lane_a_lists_every_function_on_every_pass():
    # opt only dumps what a pass changed, but the function list is how a CFG is
    # picked, so every card carries every function in the module: the ones the
    # pass touched marked changed, the rest carrying their current state so
    # their CFG is still there to look at (the viewer dims them).
    module = (
        "define i32 @a() {\n  ret i32 1\n}\n"
        "\n"
        "define i32 @b() {\n  ret i32 2\n}\n"
    )
    stderr = (
        "Running pass: NoOpPass on a\n"
        "Running pass: InstCombinePass on b\n"
        "*** IR Dump After InstCombinePass on b ***\n"
        "define i32 @b() {\n  ret i32 3\n}\n"
    )
    passes = build_lane_a(stderr, input_ir=module)

    # A pass that dumped nothing at all still lists both functions.
    noop = next(p for p in passes if p.name == "NoOpPass")
    assert list(noop.functions) == ["a", "b"]
    assert not any(c.changed for c in noop.functions.values())
    assert not noop.changed  # ...so it stays hidden by the "only changed" filter
    assert noop.functions["a"].before == noop.functions["a"].after
    assert noop.dots["a"] == (noop.dots["a"][0], noop.dots["a"][0])  # one CFG, both sides

    # A pass that changed one function marks that one and only that one.
    instcombine = next(p for p in passes if p.name == "InstCombinePass")
    assert list(instcombine.functions) == ["a", "b"]
    assert not instcombine.functions["a"].changed
    assert instcombine.functions["b"].changed
    assert instcombine.changed
    # The filled row carries the function as it stands now, not an empty before.
    assert "ret i32 1" in instcombine.functions["a"].after


def test_build_lane_a_fill_follows_a_module_pass_deleting_a_function():
    # A module dump says what the module holds; a function it dropped must not
    # keep appearing on later cards from its last known text.
    module = (
        "define i32 @a() {\n  ret i32 1\n}\n"
        "\n"
        "define i32 @b() {\n  ret i32 2\n}\n"
    )
    stderr = (
        "Running pass: GlobalDCEPass on [module]\n"
        "*** IR Dump After GlobalDCEPass on [module] ***\n"
        "define i32 @b() {\n  ret i32 2\n}\n"
        "Running pass: InstCombinePass on b\n"
        "*** IR Dump After InstCombinePass on b ***\n"
        "define i32 @b() {\n  ret i32 3\n}\n"
    )
    passes = build_lane_a(stderr, input_ir=module)
    dce = next(p for p in passes if p.name == "GlobalDCEPass")
    assert list(dce.functions) == ["[module]", "b"]  # "a" is gone as of this card
    later = next(p for p in passes if p.name == "InstCombinePass")
    assert list(later.functions) == ["b"]


def test_build_lane_a_folds_single_function_scc_dumps_into_the_function():
    # A CGSCC pass names its dump "(main)"; that is the function main, not a
    # second entity with no history of its own.
    stderr = (
        "Running pass: InstCombinePass on main\n"
        "*** IR Dump After InstCombinePass on main ***\n"
        "define i32 @main() {\n"
        "  ret i32 1\n"
        "}\n"
        "Running pass: PostOrderFunctionAttrsPass on (main)\n"
        "*** IR Dump After PostOrderFunctionAttrsPass on (main) ***\n"
        "define i32 @main() #0 {\n"
        "  ret i32 1\n"
        "}\n"
    )
    passes = build_lane_a(stderr)
    attrs = next(p for p in passes if p.name == "PostOrderFunctionAttrsPass")
    assert set(attrs.functions) == {"main"}  # not "(main)"
    assert attrs.functions["main"].before == "define i32 @main() {\n  ret i32 1\n}"

    # A real multi-function SCC is its own entity and keeps its own name.
    multi = build_lane_a(
        "Running pass: P on (a, b)\n"
        "*** IR Dump After P on (a, b) ***\n"
        "define i32 @a() {\n  ret i32 1\n}\n"
        "define i32 @b() {\n  ret i32 2\n}\n"
    )
    assert set(multi[0].functions) == {"(a, b)"}


# One module, printed at every dump because the runner passes
# -print-module-scope. The header still names the entity the pass ran on.
MODULE_SCOPE = """\
; ModuleID = 'sample.ll'
source_filename = "sample.c"
define i32 @a() {
  ret i32 %v
}

define i32 @b() {
  ret i32 2
}

!llvm.module.flags = !{}
!0 = !DIFile(filename: "sample.c", directory: "/repo")
"""


def _module_scope_dump(pass_name: str, entity: str, value: str) -> str:
    return (
        f"Running pass: {pass_name} on {entity}\n"
        f"*** IR Dump After {pass_name} on {entity} ***\n"
        + MODULE_SCOPE.replace("%v", value)
    )


SEED_MODULE = strip_module_noise(MODULE_SCOPE.replace("%v", "0").splitlines())


def test_build_lane_a_carves_the_named_entity_out_of_a_module_scope_dump():
    # Every dump is a whole module, so a function's card must show that
    # function -- not the module, and not a body truncated at the first "}".
    passes = build_lane_a(
        _module_scope_dump("SROAPass", "a", "1")
        + _module_scope_dump("InstCombinePass", "[module]", "3"),
        input_ir=SEED_MODULE,
    )
    sroa = next(p for p in passes if p.name == "SROAPass")
    assert set(sroa.functions) == {"a", "b"}
    assert sroa.functions["a"].before == "define i32 @a() {\n  ret i32 0\n}"
    assert sroa.functions["a"].after == "define i32 @a() {\n  ret i32 1\n}"
    # b was not what the pass ran on, so it is listed unchanged, not omitted.
    assert not sroa.functions["b"].changed
    # The module card still gets the whole module, bookkeeping stripped.
    combine = next(p for p in passes if p.name == "InstCombinePass")
    module = combine.functions["[module]"].after
    assert "define i32 @a()" in module and "define i32 @b()" in module
    assert "!DIFile" not in module and "ModuleID" not in module


def test_build_lane_a_folds_loop_dumps_into_the_function_they_run_in():
    # A loop pass names its dump after the loop, which is not an entity the
    # report tracks -- and under module scope the body is the whole module
    # anyway. Folding it onto the containing function gives the card a real
    # before/after instead of a fragment with nothing to diff against.
    passes = build_lane_a(
        _module_scope_dump("SROAPass", "a", "1")
        + _module_scope_dump("LoopRotatePass", "loop %h in function a", "2"),
        input_ir=SEED_MODULE,
    )
    rotate = next(p for p in passes if p.name == "LoopRotatePass")
    assert set(rotate.functions) == {"a", "b"}  # no "loop %h in function a"
    change = rotate.functions["a"]
    assert change.changed
    assert change.before == "define i32 @a() {\n  ret i32 1\n}"
    assert change.after == "define i32 @a() {\n  ret i32 2\n}"


def test_build_lane_a_maps_source_off_each_dump_s_own_metadata():
    # The mapping needs no second opt run: a module-scope dump defines the
    # !N it references, so the table is read off the very text the snapshot
    # was cut from.
    stderr = (
        "Running pass: SROAPass on a\n"
        "*** IR Dump After SROAPass on a ***\n"
        "; ModuleID = 'sample.ll'\n"
        "define i32 @a() {\n"
        "  ret i32 1, !dbg !7\n"
        "}\n"
        "!7 = !DILocation(line: 42, column: 3, scope: !8)\n"
        "!8 = distinct !DISubprogram(name: \"a\", file: !9, line: 40)\n"
        "!9 = !DIFile(filename: \"sample.c\", directory: \"/repo\")\n"
    )
    mapped = build_lane_a(stderr)[0].src_maps["a"]
    assert [None, SourceRef("/repo/sample.c", 42), None] == mapped
    assert build_lane_a(stderr, mapped=False)[0].src_maps == {}

def test_build_lane_a_marks_custom(capture):
    # --custom-pass matches case-insensitively; only the named pass is flagged.
    passes = build_lane_a(capture("opt-sample.stderr"), custom_passes=("sroapass",))
    sroa = next(p for p in passes if p.name == "SROAPass")
    assert sroa.is_custom
    assert all(not p.is_custom for p in passes if p.name != "SROAPass")



INPUT_MODULE = """; ModuleID = 'sample.ll'
source_filename = "sample.c"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() #0 !dbg !9 {
  br label %1, !dbg !12

1:
  ret i32 0, !dbg !12
}

!llvm.dbg.cu = !{!2}
!0 = !DIFile(filename: "sample.c", directory: "/repo")
!2 = distinct !DICompileUnit(file: !0)
!9 = distinct !DISubprogram(name: "main", file: !0, line: 30, unit: !2)
!12 = !DILocation(line: 33, column: 3, scope: !9)
"""


def test_build_input_pass_leads_the_lane_with_the_unoptimized_module():
    card = build_input_pass(INPUT_MODULE, Path("report/raw/sample.ll"))
    assert card.lane == "ir"
    assert card.name == INPUT_PASS_NAME
    assert card.run_index == 0  # ahead of the pipeline, which starts at 1
    assert card.pass_id is None and card.time_ms is None
    assert card.changed  # it carries IR, so "only changed" keeps it
    assert card.is_input  # the viewer withholds Diff and CFG for it

    change = card.functions[MODULE_FN]
    assert change.before == ""  # nothing precedes the input
    assert "define i32 @main() #0 !dbg !9 {" in change.after
    # Same stripping as every other card: no preamble, no metadata block.
    assert "ModuleID" not in change.after and "!DILocation" not in change.after
    # No CFG: the view is withheld for input cards, so a graph would be dead
    # weight in every report.
    assert card.dots == {}



def test_build_input_pass_serves_the_machine_lane_too():
    # Lane B's card is the same module after every opt pass -- the text llc
    # actually reads -- so it lands on the machine lane, not the IR one.
    card = build_input_pass(
        INPUT_MODULE, Path("report/raw/opt-final.ll"),
        lane="mir", name=BACKEND_INPUT_PASS_NAME, note="after every opt pass",
    )
    assert card.lane == "mir"
    assert card.name == BACKEND_INPUT_PASS_NAME
    assert card.run_index == 0 and card.is_input
    assert "backend" in card.log and "after every opt pass" in card.log
    # Still LLVM IR, so it maps through the module's own metadata as usual.
    assert card.src_maps[MODULE_FN]

def test_build_input_pass_maps_lines_through_the_modules_own_metadata():
    # The input module is self-describing, so no harvest is needed -- but the
    # table must be read before stripping removes it.
    card = build_input_pass(INPUT_MODULE, Path("in.ll"))
    mapping = card.src_maps[MODULE_FN]
    lines = card.functions[MODULE_FN].after.split("\n")
    assert len(mapping) == len(lines)
    located = {line: ref for line, ref in zip(lines, mapping) if ref}
    assert located["  ret i32 0, !dbg !12"].line == 33
    assert located["  ret i32 0, !dbg !12"].file.endswith("sample.c")
    assert located["define i32 @main() #0 !dbg !9 {"].line == 30

    # --no-source-map (mapped=False) costs nothing and maps nothing.
    assert build_input_pass(INPUT_MODULE, Path("in.ll"), mapped=False).src_maps == {}


def test_build_input_pass_without_debug_info_still_shows_the_module():
    card = build_input_pass("define i32 @main() {\n  ret i32 0\n}\n", Path("in.ll"))
    assert card.src_maps == {}
    assert "ret i32 0" in card.functions[MODULE_FN].after

def test_effective_pipeline_appends_function_passes():
    assert _effective_pipeline("default<O2>", ("mba-add",)) == "default<O2>,function(mba-add)"
    assert _effective_pipeline("mem2reg", ("mba-add", "strlen")) == \
        "mem2reg,function(mba-add),function(strlen)"
    # Dedup on the bare and wrapped forms alike.
    assert _effective_pipeline("default<O2>,function(mba-add)", ("mba-add",)) == \
        "default<O2>,function(mba-add)"
    assert _effective_pipeline("mem2reg,mba-add", ("mba-add",)) == "mem2reg,mba-add"


def test_build_lane_b_on_fixture(capture):
    passes = build_lane_b(capture("llc-carry.stderr"), asm_text="main:\n  ret\n")
    assert passes
    assert all(p.lane == "mir" for p in passes)
    assert passes[-1].pass_id == "x86-asm-printer"
    assert passes[-1].asm == "main:\n  ret\n"
    assert any(p.changed for p in passes)
    # spill detection surfaced
    assert any(p.spills and sum(p.spills.values()) > 0 for p in passes)
    # reg map attached to the rewriter pass
    rewriter = next(p for p in passes if p.pass_id == "virtregrewriter")
    assert rewriter.reg_map


# --- command sheet ---------------------------------------------------------------


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_build_commands_records_every_stage_that_ran():
    commands = build_commands(
        _Stub(kind="clang", cmd=("clang-22", "-S", "-emit-llvm", "-o", "a.ll", "a.c")),
        _Stub(cmd=("opt-22", "-passes=default<O2>", "-o", "b.ll", "a.ll")),
        _Stub(cmd=("llc-22", "-o", "a.s", "b.ll")),
    )
    assert [c["stage"] for c in commands] == ["compile", "opt", "llc"]
    assert commands[0]["argv"][0] == "clang-22"
    # The shell-quoted line is what the viewer shows and a reader pastes.
    assert commands[1]["line"] == "opt-22 '-passes=default<O2>' -o b.ll a.ll"
    assert all(c["note"] for c in commands)


def test_build_commands_omits_a_stage_that_never_ran():
    """An opt that emitted nothing means no llc, and so no llc command."""
    commands = build_commands(_Stub(kind="passthrough", cmd=("cp", "a.ll", "b.ll")), None, None)
    assert [c["stage"] for c in commands] == ["compile"]
    assert "already textual LLVM IR" in commands[0]["note"]


# --- full pipeline (needs toolchain; skips when unavailable) ----------------------


def test_build_report_end_to_end(toolchain, tmp_path):
    summary = build_report(
        SAMPLE_C,
        passes="mem2reg",
        output=tmp_path / "report",
        bin_dir=toolchain.bin_dir,
    )
    assert summary["laneACount"] > 0
    assert summary["laneBCount"] > 0
    assert not summary["optCrashed"]
    assert not summary["llcCrashed"]

    manifest = json.loads(Path(summary["manifest"]).read_text())
    assert manifest["metadata"]["source"].endswith("sample.c")
    assert manifest["metadata"]["pipeline"] == "mem2reg"
    assert manifest["metadata"]["toolVersions"]["opt"].startswith("Ubuntu LLVM version 22")
    assert {p["lane"] for p in manifest["passes"]} == {"ir", "mir"}
    # final CFG captured per lane, per function, with code-bearing labels
    final_cfg = manifest["metadata"]["finalCfg"]
    assert set(final_cfg) == {"ir", "mir"}
    for fn, dot in final_cfg["mir"].items():
        assert dot.startswith("digraph")
        assert 'name="bb.0"' in dot   # block names, in their own attribute
        assert ", label=" in dot      # ...and real instructions beside them
    assert (tmp_path / "report" / "index.html").is_file()
    assert (tmp_path / "report" / "raw" / "opt-stderr.log").is_file()
    assert (tmp_path / "report" / "data" / "pass-1.json").is_file()

    # Lane A opens on the module as clang emitted it, before any pass ran.
    first = manifest["passes"][0]
    assert first["lane"] == "ir" and first["name"] == INPUT_PASS_NAME
    assert first["runIndex"] == 0
    assert first["isInput"]

    # ... and so does lane B, on the module opt handed to llc.
    backend = next(p for p in manifest["passes"] if p["lane"] == "mir")
    assert backend["name"] == BACKEND_INPUT_PASS_NAME
    assert backend["runIndex"] == 0 and backend["isInput"]
    assert [p["name"] for p in manifest["passes"] if p["isInput"]] == \
        [INPUT_PASS_NAME, BACKEND_INPUT_PASS_NAME]
    backend_chunk = json.loads(
        (tmp_path / "report" / "data" / f"pass-{backend['id']}.json").read_text())
    assert "define" in backend_chunk["functions"][MODULE_FN]["after"]
    chunk = json.loads((tmp_path / "report" / "data" / f"pass-{first['id']}.json").read_text())
    assert chunk["functions"][MODULE_FN]["before"] == ""
    assert "define" in chunk["functions"][MODULE_FN]["after"]

    # Every stage's exact argv travels with the report.
    commands = manifest["metadata"]["commands"]
    assert [c["stage"] for c in commands] == ["compile", "opt", "llc"]
    assert commands[0]["argv"][-1].endswith("sample.c")
    assert "-passes=mem2reg" in commands[1]["argv"]
    assert "-print-after-all" in commands[2]["argv"]
