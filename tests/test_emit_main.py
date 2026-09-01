"""Tests for cli.emit (report emission) and cli.main (lane builders, pipeline)."""

from __future__ import annotations

import json
from pathlib import Path

from cli.diff import FnChange
from cli.emit import ReportPass, emit_report
from cli.main import _effective_pipeline, build_lane_a, build_lane_b, build_report

FIXTURES = Path(__file__).parent / "fixtures"
FRONTEND = Path(__file__).parent.parent / "frontend"
OPT_STDERR = (FIXTURES / "opt-sample.stderr").read_text()
LLC_CARRY = (FIXTURES / "llc-carry.stderr").read_text()


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


def test_emit_report_missing_frontend_is_tolerated(tmp_path):
    emit_report(
        tmp_path / "r", passes=_passes_fixture(),
        metadata={}, frontend_dir=tmp_path / "nope",
    )
    assert (tmp_path / "r" / "data" / "manifest.json").is_file()


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


def test_build_lane_a_on_fixture():
    passes = build_lane_a(OPT_STDERR)
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


def test_build_lane_a_marks_custom():
    # --custom-pass matches case-insensitively; only the named pass is flagged.
    passes = build_lane_a(OPT_STDERR, custom_passes=("sroapass",))
    sroa = next(p for p in passes if p.name == "SROAPass")
    assert sroa.is_custom
    assert all(not p.is_custom for p in passes if p.name != "SROAPass")


def test_effective_pipeline_appends_function_passes():
    assert _effective_pipeline("default<O2>", ("mba-add",)) == "default<O2>,function(mba-add)"
    assert _effective_pipeline("mem2reg", ("mba-add", "strlen")) == \
        "mem2reg,function(mba-add),function(strlen)"
    # Dedup on the bare and wrapped forms alike.
    assert _effective_pipeline("default<O2>,function(mba-add)", ("mba-add",)) == \
        "default<O2>,function(mba-add)"
    assert _effective_pipeline("mem2reg,mba-add", ("mba-add",)) == "mem2reg,mba-add"


def test_build_lane_b_on_fixture():
    passes = build_lane_b(LLC_CARRY, asm_text="main:\n  ret\n")
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


# --- full pipeline (needs toolchain; skips when unavailable) ----------------------


def test_build_report_end_to_end(toolchain, tmp_path):
    summary = build_report(
        FIXTURES / "sample.c",
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
        assert '\\n  ' in dot  # block names plus real instructions
    assert (tmp_path / "report" / "index.html").is_file()
    assert (tmp_path / "report" / "raw" / "opt-stderr.log").is_file()
    assert (tmp_path / "report" / "data" / "pass-1.json").is_file()
