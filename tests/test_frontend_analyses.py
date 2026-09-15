"""The Graphs pane: several graphs from the file, one at a time after a click."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The pane lives in the main-view section, but cfgBodyHtml is defined down in the
# CFG section, so it is stubbed here rather than co-sliced.
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- main view");
const end = src.indexOf("// --- Source view");
if (start < 0 || end < 0) { console.error("main view section not found"); process.exit(2); }

// These live outside the slice (views / CFG sections), and let/const inside eval
// do not leak out of it -- so they are declared here and closed over instead.
let CFG_PENDING = [];
let CURRENT_MANIFEST = null;
const STATE = { fn: null, analysisTypes: ["pdt"] };

const code =
  "const escapeHtml = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" +
  "const cfgBodyHtml = (dot, label) => {\n" +
  "  const idx = CFG_PENDING.length; CFG_PENDING.push(dot);\n" +
  "  return `<div class=\"cfg-cy\" data-idx=\"${idx}\"><span class=\"flabel\">${escapeHtml(label || '')}</span></div>`;\n" +
  "};\n" +
  src.slice(start, end);
eval(code);
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

// Two functions' worth of graphs, plus the module-wide call graph.
const G = (n) => `digraph { n0 [name="x", label="${n}"]; }`;
const manifest = { metadata: { analyses: {
  callGraph: G("CALL"),
  functions: { f: { pdt: G("PDT"), cdg: G("CDG"), ddg: G("DDG"),
                    pdg: G("PDG"), mdg: G("MDG"), lnt: G("LNT") } },
} } };

const render = (types, fn) => {
  CURRENT_MANIFEST = manifest;
  STATE.analysisTypes = types;
  STATE.fn = fn === undefined ? "f" : fn;
  CFG_PENDING = [];
  return analysesPaneHtml();
};
const drawn = () => CFG_PENDING.slice();

// One graph: the pre-existing behaviour, one body, no stack wrapper needed.
let html = render(["pdt"]);
check("one graph draws one body", drawn().length === 1);
check("one graph is a PDT", drawn()[0].includes("PDT"));
check("one graph is wrapped in the stack", html.includes('class="graph-stack"'));
check("the active chip is the selected one",
  (html.match(/class="ptab active"/g) || []).length === 1);

// Several: every selected graph gets its own body, in chip order.
html = render(["pdt", "ddg"]);
check("two graphs draw two bodies", drawn().length === 2);
check("bodies follow the chip order", drawn()[0].includes("PDT") && drawn()[1].includes("DDG"));
check("two bodies get distinct indices",
  new Set([...html.matchAll(/data-idx="(\d+)"/g)].map(m => m[1])).size === 2);
check("two chips are active",
  (html.match(/class="ptab active"/g) || []).length === 2);

// The list is canonicalised, not trusted: order is the chip row's, dupes gone.
check("input order is ignored", render(["ddg", "pdt"]) === render(["pdt", "ddg"]));
check("a duplicate draws once", (render(["pdt", "pdt"]).match(/data-idx="/g) || []).length === 1);
check("an unknown name is dropped",
  (render(["pdt", "nope"]).match(/data-idx="/g) || []).length === 1);

// The call graph comes from a different place and must not be read per function.
html = render(["cg"]);
check("cg reads the module call graph", drawn()[0].includes("CALL"));
check("cg is labelled as the call graph", html.includes("call graph"));
html = render(["pdt", "cg"]);
check("cg and a function graph coexist", drawn()[0].includes("PDT") && drawn()[1].includes("CALL"));

// Nothing selected is a hint, not an empty pane.
html = render([]);
check("nothing selected says so", html.includes("no graph selected"));
check("nothing selected draws nothing", drawn().length === 0);

// A selected graph with no data for this function: named, not silently dropped.
html = render(["pdt"], "not-a-function");
check("a missing graph is named", html.includes("no PDT"));

// Every selected graph missing collapses to one message, not one per graph.
html = render(["pdt", "ddg"], "not-a-function");
check("all missing collapses to one message",
  (html.match(/cfg-empty/g) || []).length === 1);

// The pane still bails out entirely when the manifest has no analyses at all.
CURRENT_MANIFEST = { metadata: {} };
check("no analyses at all is its own message",
  analysesPaneHtml().includes("no analyses for this module"));

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("graphs pane checks passed");
"""


def _run_node(harness: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", harness, *args],
        capture_output=True, text=True, timeout=60,
    )


def test_graphs_pane_renders_a_selection():
    result = _run_node(HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_the_analysis_chips_replace_rather_than_toggle():
    """A click focuses one graph; opening on several is the file's doing."""
    app = (FRONTEND / "app.js").read_text()
    assert ".ptab[data-analysis]" in app
    assert "STATE.analysisTypes = [t]" in app
    assert "STATE.analysisTypes.includes(t)" not in app, "the toggle is gone"


def test_the_chip_list_has_one_source_of_truth():
    """UI_VALUES used to re-list ANALYSIS_TYPES; the manifest path now does not."""
    app = (FRONTEND / "app.js").read_text()
    ui_values = app[app.index("const UI_VALUES"):app.index("function applyUiSettings")]
    assert "analysis" not in ui_values, "analysis must not go through the membership loop"
    assert "analysisSelection(ui.analysis)" in app
