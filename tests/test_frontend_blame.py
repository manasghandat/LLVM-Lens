"""Blame in the browser: reading the lineage document the report ships."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import SAMPLE_C

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"
REPO = FRONTEND.parent.parent.parent

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The blame section is self-contained: it reads the emitted document and the
# rendered row, and touches no other view code until it renders HTML.
BLAME_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- blame: which pass put each line");
const end = src.indexOf("/* --- llvm ir highlighting");
if (start < 0 || end < 0) { console.error("blame section not found"); process.exit(2); }
eval("const escapeHtml = (s) => String(s);\n" + src.slice(start, end));
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const deep = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// One lane's document, shaped exactly as blame_document emits it: pass names,
// writes and per-line chains all interned as integers.
const DOC = {
  lane: "ir",
  names: ["Input IR", "SROAPass", "GVNPass"],
  kinds: ["created", "rewritten", "renamed"],
  events: [[1, 1, 1], [4, 2, 2]],       // [run, name index, kind index]
  hist: [[], [0], [0, 1]],              // chain id -> its writes
  functions: {
    main: {
      text: ["define i32 @main() {", "  ret i32 1", "}"],
      states: [
        { run: 0, h: [0, 0, 0] },
        { run: 1, h: [0, 1, 0] },
        { run: 4, h: [0, 2, 0] },
      ],
    },
    empty: { text: [], states: [] },
  },
};

// --- interned ids come back as writes ---------------------------------------

check("a chain expands to its writes",
  deep(blameChain(DOC, 2), [
    { run: 1, name: "SROAPass", kind: "rewritten" },
    { run: 4, name: "GVNPass", kind: "renamed" },
  ]));
check("the empty chain is no writes", blameChain(DOC, 0).length === 0);
check("an unknown chain id is no writes", blameChain(DOC, 99).length === 0);

// --- the walk the views read ------------------------------------------------

const walk = blameWalkFor(DOC, "main");
check("there is a state per run that changed the function",
  deep(walk.runs.map(r => r.run), [0, 1, 4]));
check("only the last state ships its text",
  walk.runs[0].lines === null && walk.runs[1].lines === null
  && deep(walk.runs[2].lines, DOC.functions.main.text));
check("the walk's lines are the last state's", walk.lines === walk.runs[2].lines);
check("the walk's history is the last state's", walk.history === walk.runs[2].history);
check("the input state owns nothing", walk.runs[0].history[0].length === 0);
check("a line the first pass wrote has one write",
  deep(walk.runs[1].history[1], [{ run: 1, name: "SROAPass", kind: "rewritten" }]));
check("its chain grows with the next pass", walk.history[1].length === 2);
check("...oldest write first", walk.history[1][1].run === 4);
check("untouched lines stay unowned",
  walk.history[0].length === 0 && walk.history[2].length === 0);
check("each line gets its own chain array",
  walk.history[1] !== walk.history[2]);
check("an unknown function has no walk", blameWalkFor(DOC, "nope") === null);
check("a function with no states has no walk", blameWalkFor(DOC, "empty") === null);
check("a report without lineage has no walk", blameWalkFor(null, "main") === null);

// --- lookups the views use --------------------------------------------------

check("blameAt picks the state at the run", blameAt(walk, 4).run === 4);
check("blameAt falls back to the state before it", blameAt(walk, 2).run === 1);
check("blameAt before the first pass is the input", blameAt(walk, 0).run === 0);
check("blameAt past the end is the last state", blameAt(walk, 99).run === 4);
check("blameLast is the newest write", blameLast(walk.history[1]).name === "GVNPass");
check("blameLast of nothing is null", blameLast([]) === null);
check("an unowned line has no tint", blameTint(null) === "btin");
check("a written line is tinted by its run", blameTint(4) === "bt4");
check("the first run is a tint, not the no-tint", blameTint(0) === "bt0");

// --- the line a clicked row showed ------------------------------------------

const row = { querySelector: () => ({ textContent: "  %1 = add i32 1, 2" }) };
check("a row lends its text", rowText(row) === "  %1 = add i32 1, 2");
const blank = { querySelector: () => ({ textContent: "\u00a0" }) };
check("an empty row lends an empty line", rowText(blank) === "");
check("a row with no code lends nothing",
  rowText({ querySelector: () => null }) === "");

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend blame checks passed");
"""


def _run_node(harness: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", harness, *args],
        capture_output=True, text=True, timeout=60,
    )


def test_blame_walk():
    result = _run_node(BLAME_HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


# --- the walk against the real stream (needs toolchain; skips without one) ---

# Every card's diff must insert exactly the lines blame says that run wrote. The
# two live in different languages, so this is the only place their matchers meet.
AGREEMENT_HARNESS = r"""
const fs = require("fs");
const [REPO, REPORT] = process.argv.slice(1);
const src = fs.readFileSync(REPO + "/src/llvm_lens/frontend/app.js", "utf8");
eval(src.slice(src.indexOf("/* --- diff helpers"), src.indexOf("/* --- blame: which pass")));
const rd = (p) => JSON.parse(fs.readFileSync(REPORT + "/" + p, "utf8"));
const manifest = rd("data/manifest.json");
const bad = [], missing = [];
let checked = 0;
for (const lane of ["ir", "mir"]) {
  let doc; try { doc = rd(`data/blame-${lane}.json`); } catch { continue; }
  const byRun = new Map(manifest.passes.filter(p => p.lane === lane).map(p => [p.runIndex, p]));
  for (const [fn, entry] of Object.entries(doc.functions)) {
    // A line was written at a state iff its chain ends on that state's run.
    const wrote = entry.states.map(s => s.h.filter(id => {
      const c = doc.hist[id];
      return c.length && doc.events[c[c.length - 1]][0] === s.run;
    }).length);
    for (let k = 1; k < entry.states.length; k++) {
      const run = entry.states[k].run;
      const card = byRun.get(run);
      const ch = card && (rd(`data/pass-${card.id}.json`).functions || {})[fn];
      if (!ch) { missing.push(`${lane}/${fn}@${run}`); continue; }
      const adds = diffOps(ch.before, ch.after).filter(o => o.op === "+").length;
      checked++;
      if (adds !== wrote[k]) {
        bad.push(`${lane}/${fn}@${run} (${card.name}): diff inserts ${adds}, blame claims ${wrote[k]}`);
      }
    }
  }
}
if (!checked) { console.error("no states checked"); process.exit(2); }
if (bad.length) { console.error("FAIL: " + bad.slice(0, 6).join(" | ")); process.exit(1); }
console.log(`blame agrees with the diff on ${checked} card/function pairs`);
"""


def test_the_diff_shows_exactly_what_blame_claims(toolchain, tmp_path):
    from llvm_lens.report import build_report

    summary = build_report(
        SAMPLE_C, output=tmp_path / "report", bin_dir=toolchain.bin_dir, source_map=False,
    )
    assert summary["laneACount"] > 0 and summary["laneBCount"] > 0
    result = _run_node(
        AGREEMENT_HARNESS, str(REPO), str(tmp_path / "report"),
    )
    assert result.returncode == 0, result.stderr
    assert "card/function pairs" in result.stdout
