"""Frontend CFG rendering checks.

Runs the browser-free parts of frontend/app.js under node: DOT parsing
(parseDot), the diff pane coloring (paneHtml), and — using the vendored UMD
builds — a headless cytoscape + dagre layout of a CFG with a loop. Skipped
when node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Harness: extract the CFG section of app.js, eval it, and exercise parseDot.
# app.js must keep the CFG section between "/* --- CFG" and "/* --- boot".
PARSE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- CFG");
const end = src.indexOf("/* --- boot");
if (start < 0 || end < 0) { console.error("CFG section not found"); process.exit(2); }
const code =
  "const escapeHtml = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" +
  src.slice(start, end);
eval(code);
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

// exact format cli/cfg.py emits: label (name + indented, truncated code) plus
// a code attribute with the full untruncated block body
const coded = 'digraph {\n  rankdir="TB";\n  n0 [label="bb.0\\n  %0 = MOV32rm", code="%0 = MOV32rm\\nJCC_1 %bb.1"];\n  n1 [label="bb.1\\n  RET 0"];\n  n0 -> n1;\n}';
const cl = parseDot(coded);
check("nodes parsed", cl.nodes.length === 2);
check("label unescaped to real newlines", cl.nodes[0].label === "bb.0\n  %0 = MOV32rm");
check("code attribute parsed", cl.nodes[0].code === "%0 = MOV32rm\nJCC_1 %bb.1");
check("node without code -> empty string", cl.nodes[1].code === "");
check("edges parsed", cl.edges.length === 1 && cl.edges[0][0] === 0 && cl.edges[0][1] === 1);

// indentation + escaped quotes in both attributes
const quoted = 'digraph {\n  rankdir="TB";\n  n0 [label="a\\"b", code="c\\"d"];\n}';
check("escaped quote in label", parseDot(quoted).nodes[0].label === 'a"b');
check("escaped quote in code", parseDot(quoted).nodes[0].code === 'c"d');

// empty graph parses to no nodes
check("empty dot -> no nodes", parseDot('digraph {\n  rankdir="TB";\n}').nodes.length === 0);

// labelBox sizes each node to its label (monospace metrics): boxes grow
// with content, width is capped at CFG_FONT.maxW, and long lines re-wrap
const short = labelBox("bb.0");
const long = labelBox("bb.0\n  " + "x".repeat(52));
check("short label box is small but not tiny", short.w > 40 && short.h >= 30);
check("box height grows with lines", long.h > short.h);
check("box width capped at maxW", long.w <= 300 + 2 * 10 + 1);  // CFG_FONT.maxW + 2*padX
check("long line re-wrapped to fit", long.wrapped.split("\n").length === 3);  // bb.0 + 2 wrapped pieces
check("multi-line label preserved", labelBox("a\nb\nc").wrapped.split("\n").length === 3);

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend parseDot checks passed");
"""

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
check("removed line marked del in before pane", b.includes('class="del"'));
check("added lines marked add in after pane", a.includes('class="add"'));
check("no add marks in before pane", !b.includes('class="add"'));
check("no del marks in after pane", !a.includes('class="del"'));
check("exactly one removed line", (b.match(/class="del"/g) || []).length === 1);
check("exactly two added lines", (a.match(/class="add"/g) || []).length === 2);
check("kept text survives tokenizing", b.replace(/<[^>]+>/g, "") === "int x;\nint y;\nreturn 0;");
check("numbers tokenized", b.includes('class="tok-num"') && a.includes('class="tok-num"'));

// tokenizer: one realistic IR line produces each token class
const hl = highlightIR("loop:\n  %r = add i32 %a, 1  ; comment");
check("ir: block label", hl.includes('<span class="tok-label">loop:</span>'));
check("ir: keyword", hl.includes('<span class="tok-kw">add</span>'));
check("ir: type", hl.includes('<span class="tok-type">i32</span>'));
check("ir: vars", hl.includes('<span class="tok-var">%r</span>') && hl.includes('<span class="tok-var">%a</span>'));
check("ir: number", hl.includes('<span class="tok-num">1</span>'));
check("ir: comment", hl.includes('<span class="tok-com">; comment</span>'));
check("ir: escape safety", highlightIR('"a<b>" ; x').includes('&lt;'));

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend diff checks passed");
"""

# Harness for the vendored graph stack: load cytoscape + dagre + the
# cytoscape-dagre UMD registration under node, run a headless dagre layout on
# a cyclic CFG, and verify ranks plus the back-edge classification used by
# mountCfg. cytoscape-dagre's UMD does require("dagre") in node, which the
# harness satisfies by monkeypatching _resolveFilename (Module.globalPaths is
# not honored by node -e).
CYTOSCAPE_HARNESS = r"""
const fs = require("fs");
const path = require("path");

const vendor = process.argv[1];
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

const Module = require("module");
const orig = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "dagre") return path.join(vendor, "dagre.min.js");
  return orig.call(this, request, ...rest);
};

const cytoscape = require(path.join(vendor, "cytoscape.min.js"));
const register = require(path.join(vendor, "cytoscape-dagre.js"));
check("dagre layout registered", typeof cytoscape === "function" && typeof register === "function");
register(cytoscape);

// a -> b -> c -> a (loop): dagre ranks a above b above c; c -> a runs back.
const cy = cytoscape({
  headless: true,
  styleEnabled: false,
  elements: [
    { data: { id: "n0", label: "a" } },
    { data: { id: "n1", label: "b" } },
    { data: { id: "n2", label: "c" } },
    { data: { id: "e01", source: "n0", target: "n1" } },
    { data: { id: "e12", source: "n1", target: "n2" } },
    { data: { id: "e20", source: "n2", target: "n0" } },
  ],
});
cy.layout({ name: "dagre", rankDir: "TB" }).run();
const y = (id) => cy.getElementById(id).position("y");
check("layout positions computed", [y("n0"), y("n1"), y("n2")].every(v => isFinite(v)));
check("ranks top-down: a < b < c", y("n0") < y("n1") && y("n1") < y("n2"));

// Same back-edge rule mountCfg applies after layout.
const back = cy.edges().filter(e => e.target().position("y") <= e.source().position("y") + 1);
check("loop edge classified as back edge", back.length === 1 && back[0].id() === "e20");
cy.destroy();

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("headless cytoscape dagre checks passed");
"""


def _run_node(harness: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", harness, *args],
        capture_output=True, text=True, timeout=60,
    )


def test_cfg_parse_dot():
    result = _run_node(PARSE_HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_diff_renderer():
    result = _run_node(DIFF_HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_cytoscape_dagre_layout():
    vendor = FRONTEND / "vendor"
    assert (vendor / "cytoscape.min.js").is_file(), "vendor files not downloaded"
    result = _run_node(CYTOSCAPE_HARNESS, str(vendor))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
