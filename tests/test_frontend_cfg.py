"""Frontend rendering checks that need no browser."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

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

const coded = 'digraph {\n  rankdir="TB";\n  n0 [name="bb.0", label="%0 = MOV32rm", code="%0 = MOV32rm\\nJCC_1 %bb.1"];\n  n1 [name="bb.1"];\n  n0 -> n1;\n}';
const cl = parseDot(coded);
check("nodes parsed", cl.nodes.length === 2);
check("block name read from its own attribute", cl.nodes[0].name === "bb.0");
check("label is code only, no block name in it", cl.nodes[0].label === "%0 = MOV32rm");
check("code attribute unescaped to real newlines", cl.nodes[0].code === "%0 = MOV32rm\nJCC_1 %bb.1");
check("node without code -> empty string", cl.nodes[1].code === "");
check("block with no instructions has an empty label", cl.nodes[1].label === "");
check("...but is still named", cl.nodes[1].name === "bb.1");
check("edges parsed", cl.edges.length === 1 && cl.edges[0][0] === 0 && cl.edges[0][1] === 1);

const legacy = parseDot('digraph {\n  n0 [label="bb.0\\n  %0 = MOV32rm", code="%0 = MOV32rm"];\n}');
check("legacy name from the first label line", legacy.nodes[0].name === "bb.0");
check("legacy label drops the name line", legacy.nodes[0].label === "  %0 = MOV32rm");

// escaped quotes in every attribute
const quoted = 'digraph {\n  rankdir="TB";\n  n0 [name="n\\"m", label="a\\"b", code="c\\"d"];\n}';
check("escaped quote in name", parseDot(quoted).nodes[0].name === 'n"m');
check("escaped quote in label", parseDot(quoted).nodes[0].label === 'a"b');
check("escaped quote in code", parseDot(quoted).nodes[0].code === 'c"d');

// empty graph parses to no nodes
check("empty dot -> no nodes", parseDot('digraph {\n  rankdir="TB";\n}').nodes.length === 0);

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

DIFF_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- diff helpers");
const end = src.indexOf("/* --- views");
if (start < 0 || end < 0) { console.error("diff section not found"); process.exit(2); }
const code =
  "const escapeHtml = (s) => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));\n" +
  src.slice(start, end);
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

check("mir: byte offset stripped",
      !highlightIR("80B      CLFLUSH %5:gr64").includes("80B"));
check("mir: instruction kept after offset strip",
      highlightIR("80B      CLFLUSH %5:gr64").includes('<span class="tok-op">CLFLUSH</span>'));
check("mir: multi-digit offset stripped",
      !highlightIR("224B      JMP_1 %bb.2").includes("224B"));
check("mir: debug-location ref stripped",
      !highlightIR("  %0 = ADD64ri32 %0, 1, debug-location !78; foo.c:30:5").includes("debug-location"));
check("mir: resolved source comment stripped",
      !highlightIR("  %0 = ADD64ri32 %0, 1, debug-location !78; foo.c:30:5").includes("foo.c"));
check("mir: instruction kept after debug-location strip",
      highlightIR("  %0 = ADD64ri32 %0, 1, debug-location !78; foo.c:30:5")
        .includes('<span class="tok-var">%0</span>'));
check("mir: debug-location without source comment stripped",
      !highlightIR("  %0 = ADD64ri32 %0, 1, debug-location !78").includes("debug-location"));
check("mir: predecessors comment filtered",
      isMirDebugLine("    ; predecessors: %bb.1 80B 96B"));
check("mir: plain instruction untouched",
      highlightIR("  %5:gr64 = MOV64mr $rsp, 1, $noreg, 0, $noreg, $rax")
        .includes('<span class="tok-var">%5</span>'));
check("mir: ordinary semicolon comment untouched",
      highlightIR("  %0 = ADD64ri32 %0, 1  ; some note").includes('<span class="tok-com">; some note</span>'));

check("mir: DBG_VALUE filtered",
      isMirDebugLine("  DBG_VALUE $rdi, $noreg, !\"ptr\", !DIExpression(), debug-location !59; x.c:0 line no:12"));
check("mir: DBG_VALUE_LIST filtered",
      isMirDebugLine("  DBG_VALUE_LIST !\"start\", !DIExpression(DW_OP_LLVM_arg, 0), $rcx, debug-location !59; x.c:0 line no:13"));
check("mir: DBG_INSTR_REF filtered",
      isMirDebugLine("  DBG_INSTR_REF !\"start\", !DIExpression(DW_OP_LLVM_arg, 0), dbg-instr-ref(1, 0), debug-location !59; x.c:0 line no:13"));
check("mir: DBG_PHI filtered",
      isMirDebugLine("  DBG_PHI $rax, 1"));
check("mir: CFI_INSTRUCTION filtered",
      isMirDebugLine("  frame-setup CFI_INSTRUCTION def_cfa_offset 16"));
check("mir: bare CFI directive filtered",
      isMirDebugLine("  CFI_INSTRUCTION offset $rbx, -32"));
check("mir: predecessors filtered by isMirDebugLine",
      isMirDebugLine("    ; predecessors: %bb.0"));
check("mir: debug-instr-number stripped",
      !highlightIR("  renamable $rbx = SUB64rr killed renamable $rbx(tied-def 0), killed renamable $rax, implicit-def dead $eflags, debug-instr-number 1, debug-location !141; x.c:37:22").includes("debug-instr-number"));
check("mir: instruction kept after debug-instr-number strip",
      highlightIR("  renamable $rbx = SUB64rr killed renamable $rbx(tied-def 0), killed renamable $rax, implicit-def dead $eflags, debug-instr-number 1, debug-location !141; x.c:37:22")
        .includes('<span class="tok-var">$rbx</span>'));
check("mir: frame-setup prefix kept on real instruction",
      highlightIR("  frame-setup PUSH64r killed $rbp, implicit-def $rsp, implicit $rsp, debug-location !140; x.c:37:15")
        .includes("frame-setup"));
check("mir: frame-setup instruction debug-location stripped",
      !highlightIR("  frame-setup PUSH64r killed $rbp, implicit-def $rsp, implicit $rsp, debug-location !140; x.c:37:15").includes("debug-location"));

// the real LLVM 22 shape: debug-location followed by :: (...) memoperand and ; file:col
check("mir: debug-location with memoperand + source tail stripped",
      !highlightIR("  MOV64mr %stack.0, 1, $noreg, 32, $noreg, killed %4:gr64, debug-location !148 :: (store (s64) into %ir.5, align 16); pressure.c:71:17").includes("debug-location"));
check("mir: resolved source tail stripped with memoperand",
      !highlightIR("  MOV64mr %stack.0, 1, $noreg, 32, $noreg, killed %4:gr64, debug-location !148 :: (store (s64) into %ir.5, align 16); pressure.c:71:17").includes("pressure.c"));
check("mir: instruction kept after memoperand + source strip",
      highlightIR("  MOV64mr %stack.0, 1, $noreg, 32, $noreg, killed %4:gr64, debug-location !148 :: (store (s64) into %ir.5, align 16); pressure.c:71:17")
        .includes("MOV64mr"));

// IR debug-info stripping
check("ir: !dbg ref stripped from instruction",
      !highlightIR("  %3 = load i64, ptr %0, align 8, !dbg !36").includes("!dbg"));
check("ir: !dbg ref stripped from define",
      !highlightIR("define dso_local i64 @mix16(ptr noundef %0) !dbg !26 {").includes("!dbg"));
check("ir: !dbg ref stripped from global",
      !highlightIR('@.str = private unnamed_addr constant [17 x i8] c"x\\0A\\00", align 1, !dbg !0').includes("!dbg"));
check("ir: instruction kept after !dbg strip",
      highlightIR("  %3 = load i64, ptr %0, align 8, !dbg !36").includes('<span class="tok-var">%3</span>'));
check("ir: #dbg_declare whole line dropped",
      isIrDebugLine("  #dbg_declare(ptr %3, !33, !DIExpression(), !34)"));
check("ir: #dbg_value whole line dropped",
      isIrDebugLine("  #dbg_value(ptr %0, !33, !DIExpression(), !34)"));
check("ir: call void @llvm.dbg.value whole line dropped",
      isIrDebugLine("  call void @llvm.dbg.value(metadata ptr %4, metadata !34, metadata !DIExpression()), !dbg !35"));
check("ir: plain instruction not dropped",
      !isIrDebugLine("  %3 = load i64, ptr %0, align 8, !dbg !36"));
check("dropDebugLine drops ir debug line",
      dropDebugLine("  #dbg_declare(ptr %3, !33, !DIExpression(), !34)"));
check("dropDebugLine drops mir debug line",
      dropDebugLine("  DBG_VALUE $rdi, $noreg, !\"seed\", !DIExpression(), debug-location !34"));
// the Optimized IR card lives in the mir tab but is ir content — still stripped
check("dropDebugLine strips ir dbg from mir-tab optimized-ir card",
      dropDebugLine("  #dbg_value(ptr %0, !33, !DIExpression(), !34)"));
check("dropDebugLine keeps a real instruction",
      !dropDebugLine("  %3 = load i64, ptr %0, align 8, !dbg !36"));

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


MODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("// --- input cards (report.build_input_pass) ---");
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
