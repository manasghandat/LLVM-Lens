"""Frontend rendering checks that need no browser.

Runs the browser-free parts of frontend/app.js under node: DOT parsing
(parseDot), the diff builder and its two renderers (diffOps / diffHunks /
unifiedDiffHtml for the Diff view, irSideHtml for the IR view), the C
tokenizer behind the Source view, and — using the vendored UMD builds — a
headless cytoscape + dagre layout of a CFG with a loop. Skipped when node is
not installed.
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

# Harness for the diff helpers: the op stream with before/after line numbers,
# hunking with context, and both renderers built on it — the unified
# (git-style) Diff view and the IR view's two whole-snapshot sides.
DIFF_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- diff helpers");
const end = src.indexOf("/* --- views");
if (start < 0 || end < 0) { console.error("diff section not found"); process.exit(2); }
const code =
  "const escapeHtml = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" +
  src.slice(start, end);
// Function declarations in a sloppy-mode eval leak into this scope, but
// `const` does not: re-read DIFF_CONTEXT from the eval completion value.
const DIFF_CONTEXT = eval(code + "\nDIFF_CONTEXT;");
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

const ops = diffOps("int x;\nint y;\nreturn 0;", "int x;\nint z;\nint w;\nreturn 0;");
const marks = ops.map(o => o.op).join("");
check("one removal, two additions, two context lines", marks === " -++ ");
check("removals precede additions in a change block", marks.indexOf("-") < marks.indexOf("+"));
check("context keeps both line numbers", ops[0].a === 1 && ops[0].b === 1);
check("removed line has no after number", ops[1].a === 2 && ops[1].b === null);
check("added lines have no before number", ops[2].a === null && ops[2].b === 2);
check("trailing context renumbered per side", ops[4].a === 3 && ops[4].b === 4);
check("removed text carried", ops[1].text === "int y;");

const stat = diffStat("a\nb", "a\nc");
check("stat counts one del and one add", stat.del === 1 && stat.add === 1);

// A change in a long function collapses to one hunk with DIFF_CONTEXT lines
// of context on each side; the rest is hidden.
const long = Array.from({ length: 40 }, (_, i) => "line " + i);
const edited = long.slice();
edited[20] = "line 20 changed";
const bigOps = diffOps(long.join("\n"), edited.join("\n"));
const hunks = diffHunks(bigOps, DIFF_CONTEXT);
check("one hunk for one edit", hunks.length === 1);
check("hunk is context + change only", hunks[0].rows.length === 2 * DIFF_CONTEXT + 2);
check("hunk reports hidden lines", hunks[0].hidden === 20 - DIFF_CONTEXT);
check("hunk header ranges", hunks[0].header === "@@ -18,7 +18,7 @@");
check("full context yields a single whole-function hunk",
      diffHunks(bigOps, Infinity)[0].rows.length === 41);
check("unchanged text has no hunks", diffHunks(diffOps("a\nb", "a\nb")).length === 0);

// Empty "before" (a function's first snapshot) is all additions, not a diff
// against one empty line.
const fresh = diffOps("", "a\nb");
check("empty before -> only additions", fresh.map(o => o.op).join("") === "++");
check("trailing newline is not a phantom line",
      diffOps("a\n", "a\nb\n").filter(o => o.op === "+").length === 1);

const html = unifiedDiffHtml(hunks);
check("hunk header rendered", html.includes("@@ -18,7 +18,7 @@"));
check("hidden-line note rendered", html.includes("17 unchanged lines hidden"));
check("added row marked", html.includes('class="urow add"'));
check("removed row marked", html.includes('class="urow del"'));
check("context rows marked", html.includes('class="urow uctx"'));
check("no bare ctx class (collides with the view's .ctx rule)",
      !html.includes('class="urow ctx"'));
check("line-number gutters emitted", (html.match(/class="uln"/g) || []).length === 2 * 8);
check("markers emitted", html.includes(">+<") && html.includes(">-<"));
check("code tokenized inside rows", html.includes('class="tok-num"'));

const irHunks = diffHunks(diffOps("  %1 = add i32 %a, 1", "  %1 = mul i32 %a, 2"));
check("ir rows keep token highlighting",
      unifiedDiffHtml(irHunks).includes('<span class="tok-kw">mul</span>'));
check("escape safety in rows", unifiedDiffHtml(diffHunks(diffOps("a", '"x<y>"')))
      .includes("&lt;"));

// IR view: each side keeps every line of its own snapshot, numbered on that
// side, with only that side's changes tinted and no marker column.
const beforeSide = irSideHtml(ops, "-");
const afterSide = irSideHtml(ops, "+");
const rowCount = (h) => (h.match(/class="urow /g) || []).length;
check("before side has a row per before line", rowCount(beforeSide) === 3);
check("after side has a row per after line", rowCount(afterSide) === 4);
check("before side tints removals only",
      beforeSide.includes('class="urow del"') && !beforeSide.includes('class="urow add"'));
check("after side tints additions only",
      afterSide.includes('class="urow add"') && !afterSide.includes('class="urow del"'));
check("before side numbers the before text",
      beforeSide.includes('<span class="uln">3</span>') && !beforeSide.includes('>4<'));
check("after side numbers the after text", afterSide.includes('<span class="uln">4</span>'));
check("ir sides have no marker column", !beforeSide.includes('class="umark"'));
check("ir sides keep token highlighting",
      irSideHtml(diffOps("", "  %1 = add i32 %a, 1"), "+")
        .includes('<span class="tok-kw">add</span>'));
check("a missing snapshot yields no rows", irSideHtml(diffOps("", "a"), "-") === "");
check("an unchanged function still renders both sides",
      rowCount(irSideHtml(diffOps("a\nb", "a\nb"), "-")) === 2
      && rowCount(irSideHtml(diffOps("a\nb", "a\nb"), "+")) === 2);

// tokenizer: one realistic IR line produces each token class
const hl = highlightIR("loop:\n  %r = add i32 %a, 1  ; comment");
check("ir: block label", hl.includes('<span class="tok-label">loop:</span>'));
check("ir: keyword", hl.includes('<span class="tok-kw">add</span>'));
check("ir: type", hl.includes('<span class="tok-type">i32</span>'));
check("ir: vars", hl.includes('<span class="tok-var">%r</span>') && hl.includes('<span class="tok-var">%a</span>'));
check("ir: number", hl.includes('<span class="tok-num">1</span>'));
check("ir: comment", hl.includes('<span class="tok-com">; comment</span>'));
check("ir: escape safety", highlightIR('"a<b>" ; x').includes('&lt;'));

// C tokenizer: the Source view renders the original file beside the IR.
const c = highlightC('  for (int i = 0; i < 0x40; i++) // loop');
check("c: keyword", c.includes('<span class="tok-kw">for</span>'));
check("c: type", c.includes('<span class="tok-type">int</span>'));
check("c: hex number", c.includes('<span class="tok-num">0x40</span>'));
check("c: comment", c.includes('<span class="tok-com">// loop</span>'));
check("c: call name", highlightC('printf("hi");').includes('<span class="tok-fn">printf</span>'));
check("c: a keyword inside a string stays plain",
      highlightC('char *s = "for while";').includes('<span class="tok-str">"for while"</span>'));
check("c: escape safety", highlightC('a < b && c > d').includes('&lt;'));

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend diff checks passed");
"""


# Harness for the view-mode gating: which of the four view chips a given pass
# can show. The input card (build_input_pass) has no predecessor and its
# snapshot is a whole module, so Diff and CFG are withheld there.
MODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("// --- input cards (cli/main.py build_input_pass) ---");
const end = src.indexOf("function fnNames()");
if (start < 0 || end < 0) { console.error("mode section not found"); process.exit(2); }
let SUMMARY = null;
const STATE = { mode: "diff" };
const currentPassSummary = () => SUMMARY;
eval(src.slice(start, end));

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const MODES = ["cfg", "diff", "ir", "src"];

// A real pass offers everything.
SUMMARY = { id: 5, name: "SROAPass", isInput: false };
check("a real pass offers all four views", MODES.every(modeAvailable));
check("a real pass renders the mode the user picked", effectiveMode() === "diff");

check("a real pass is not an input card", !isInputCard());

// Either lane's input card withholds the two that cannot mean anything for it.
SUMMARY = { id: 83, lane: "mir", name: "Optimized IR", isInput: true, runIndex: 0 };
check("the machine lane's card is an input card too", isInputCard());
check("machine input card withholds diff and cfg",
      !modeAvailable("diff") && !modeAvailable("cfg"));
SUMMARY = { id: 1, lane: "ir", name: "Input IR", isInput: true, runIndex: 0 };
check("the ir lane's card is an input card", isInputCard());
check("input card withholds diff", !modeAvailable("diff"));
check("input card withholds cfg", !modeAvailable("cfg"));
check("input card keeps ir and src", modeAvailable("ir") && modeAvailable("src"));
check("input card falls back to ir", effectiveMode() === "ir");
check("the requested mode is not clobbered", STATE.mode === "diff");
STATE.mode = "src";
check("a supported mode is kept on the input card", effectiveMode() === "src");

// Stepping back onto a real pass restores what the user had asked for.
STATE.mode = "diff";
SUMMARY = { id: 5, name: "SROAPass", isInput: false };
check("mode restored when leaving the input card", effectiveMode() === "diff");

// Older reports have no isInput field; nothing is withheld.
SUMMARY = { id: 5, name: "SROAPass" };
check("a report without isInput offers everything", MODES.every(modeAvailable));
SUMMARY = null;
check("no pass selected offers everything", MODES.every(modeAvailable));

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend mode checks passed");
"""


FILTER_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf('// --- "only changed" (both lanes) ---');
const end = src.indexOf("function renderPassList()");
if (start < 0 || end < 0) { console.error("filter section not found"); process.exit(2); }
eval(src.slice(start, end));

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

// Off, everything shows.
check("filter off keeps an unchanged ir pass",
      passVisible({ lane: "ir", changed: false }, false));
check("filter off keeps an unchanged machine pass",
      passVisible({ lane: "mir", changed: false }, false));

// On, the rule is the same in both lanes -- this is what was broken for mir.
check("filter drops an unchanged machine pass",
      !passVisible({ lane: "mir", changed: false }, true));
check("filter drops an unchanged ir pass",
      !passVisible({ lane: "ir", changed: false }, true));
check("filter keeps a changed machine pass",
      passVisible({ lane: "mir", changed: true }, true));
check("filter keeps a changed ir pass",
      passVisible({ lane: "ir", changed: true }, true));

// Exemptions.
check("filter keeps an unchanged custom machine pass",
      passVisible({ lane: "mir", changed: false, isCustom: true }, true));
check("filter keeps an unchanged custom ir pass",
      passVisible({ lane: "ir", changed: false, isCustom: true }, true));
check("filter keeps each lane's input card",
      passVisible({ lane: "mir", changed: true, isInput: true }, true) &&
      passVisible({ lane: "ir", changed: true, isInput: true }, true));

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend filter checks passed");
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


def test_view_mode_gating():
    result = _run_node(MODE_HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_only_changed_filter_applies_to_both_lanes():
    """The machine lane used to bypass the filter entirely."""
    result = _run_node(FILTER_HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_cytoscape_dagre_layout():
    vendor = FRONTEND / "vendor"
    assert (vendor / "cytoscape.min.js").is_file(), "vendor files not downloaded"
    result = _run_node(CYTOSCAPE_HARNESS, str(vendor))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
