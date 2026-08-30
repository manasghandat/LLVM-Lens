"""Frontend CFG graph renderer checks.

Runs the pure layout/SVG functions from frontend/app.js under node (the
browser-free parts: parseDot, layoutCfg, cfgSvgHtml). Skipped when node is
not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Harness: extract the CFG section of app.js, eval it, and exercise it.
# app.js must keep the CFG section between "const CFG = {" and "/* --- boot".
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("const CFG = {");
const end = src.indexOf("/* --- boot");
if (start < 0 || end < 0) { console.error("CFG section not found"); process.exit(2); }
const code =
  "const escapeHtml = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" +
  src.slice(start, end);
eval(code);
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

// loop graph: back edge + self-loop
const loop = 'digraph {\n  rankdir="TB";\n  n0 [label="entry"];\n  n1 [label="loop"];\n  n2 [label="exit"];\n  n0 -> n1;\n  n1 -> n2;\n  n1 -> n1;\n}';
const g = parseDot(loop);
const laid = layoutCfg(g);
const svg = cfgSvgHtml(loop);
check("nodes parsed", g.nodes.length === 3);
check("edges parsed", g.edges.length === 3);
check("self-loop flagged as back edge", laid.back.has("1>1"));
check("forward edge not back", !laid.back.has("0>1"));
check("all nodes positioned in bounds", g.nodes.every(nd => {
  const p = laid.pos[nd.id]; return p.x >= 0 && p.y >= 0;
}));
check("svg emits an edge marker per edge", (svg.match(/marker-end/g) || []).length === 3);
check("entry node styled", svg.includes('class="node entry"'));
check("back edge styled red", svg.includes('class="back"'));

// diamond graph: layered layout, entry first
const diamond = 'digraph {\n  n0 [label="a"];\n  n1 [label="b"];\n  n2 [label="c"];\n  n3 [label="d"];\n  n0 -> n1;\n  n0 -> n2;\n  n1 -> n3;\n  n2 -> n3;\n}';
const d = parseDot(diamond);
const dl = layoutCfg(d);
check("diamond: entry at top", dl.pos[0].y <= dl.pos[1].y && dl.pos[0].y <= dl.pos[2].y);
check("diamond: exits at bottom", dl.pos[3].y >= dl.pos[1].y && dl.pos[3].y >= dl.pos[2].y);
check("diamond: svg has 4 nodes", (cfgSvgHtml(diamond).match(/<g class="node/g) || []).length === 4);

// escaped quote + indentation (exact format cli/cfg.py emits)
const quoted = 'digraph {\n  rankdir="TB";\n  n0 [label="a\\"b"];\n}';
check("indented lines parsed", parseDot(quoted).nodes[0].label === 'a"b');

// multi-line labels: block name + instructions (cli/cfg.py emits \n escapes)
const coded = 'digraph {\n  rankdir="TB";\n  n0 [label="bb.0\\n  %0 = MOV32rm\\n  JCC_1 %bb.1"];\n  n1 [label="bb.1\\n  RET 0"];\n  n0 -> n1;\n}';
const cl = parseDot(coded);
check("multi-line label has 3 lines", cl.nodes[0].label.split("\n").length === 3);
check("first line is the block name", cl.nodes[0].label.split("\n")[0] === "bb.0");
check("code line indented", cl.nodes[0].label.split("\n")[1] === "  %0 = MOV32rm");
check("wraps code lines, not the name", wrapLabel(cl.nodes[0].label, 22)[0] === "bb.0");
const csvg = cfgSvgHtml(coded);
check("block name tspan styled", csvg.includes('tspan class="bn"'));
check("code tspan styled", csvg.includes('tspan class="c"'));

// empty graph renders nothing
check("empty dot -> empty svg", cfgSvgHtml('digraph {\n  rankdir="TB";\n}') === "");

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend CFG checks passed");
"""


def test_cfg_renderer():
    result = subprocess.run(
        ["node", "-e", HARNESS, str(FRONTEND / "app.js")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


# Harness for the diff helpers: additions must be green ("add") in the after
# pane and deletions red ("del") in the before pane.
DIFF_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- diff helpers");
const end = src.indexOf("/* --- views");
if (start < 0 || end < 0) { console.error("diff section not found"); process.exit(2); }
const code =
  "const escapeHtml = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" +
  src.slice(start, end);
eval(code);
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

const d = diffLines("int x;\nint y;\nreturn 0;", "int x;\nint z;\nint w;\nreturn 0;");
const b = paneHtml(d.before, "del");
const a = paneHtml(d.after, "add");
check("removed line red in before pane", b.includes('class="del"'));
check("added lines green in after pane", a.includes('class="add"'));
check("no green in before pane", !b.includes('class="add"'));
check("no red in after pane", !a.includes('class="del"'));
check("kept lines uncolored", b.split('class="').length - 1 === 1);

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend diff checks passed");
"""


def test_diff_renderer():
    result = subprocess.run(
        ["node", "-e", DIFF_HARNESS, str(FRONTEND / "app.js")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
