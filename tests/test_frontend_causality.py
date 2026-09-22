"""Causes in the browser: reading data/causality.json onto the cards."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The causality section reads the document and the card on show; everything
# else it touches is stubbed here.
HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("/* --- causality (causality.py");
const end = src.indexOf("/* --- bottom panel");
if (start < 0 || end < 0) { console.error("causality section not found"); process.exit(2); }
let SUMMARY = null, RUN = null;
const escapeHtml = (s) => String(s);
const currentPassSummary = () => SUMMARY;
const cardRun = () => RUN;
const passSummaries = () => CARDS;
const CURRENT_MANIFEST = { metadata: { hasCausality: true } };
const loadJSON = async () => DOC;
const node = (key, name, runs, changed = true) => ({
  key, pass: name + "Pass", name, unit: "function @f", runs, changedRuns: changed ? runs : [],
  entities: ["f"], changed, finalDiffers: true, inBaseline: true, error: null });
// a -> b -> c, a -> c (explained by b); c pre-empts d, which changed nothing.
const DOC = {
  nodes: [node("a", "A", [3]), node("b", "B", [5]), node("c", "C", [8, 9]),
          node("d", "D", [12], false)],
  edges: [
    { from: 0, to: 1, kind: "enables", fns: ["f"], direct: true },
    { from: 1, to: 2, kind: "enables", fns: ["f"], direct: true },
    { from: 0, to: 2, kind: "enables", fns: ["f"], direct: false },
    { from: 2, to: 3, kind: "preempts", fns: ["f"], direct: true },
  ],
};
const CARDS = [
  { id: 1, lane: "ir", name: "Input IR", isInput: true, runs: [0] },
  { id: 2, lane: "ir", name: "APass", runs: [3] },
  { id: 3, lane: "ir", name: "CPass", runs: [8, 9] },
  { id: 4, lane: "ir", name: "DPass", runs: [14] },
];
eval(src.slice(start, end));
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const deep = (a, b) => JSON.stringify(a) === JSON.stringify(b);

loadCausality().then(() => {
  check("the chain to c runs through b", deep(causalChain(2), [0, 1, 2]));
  check("a node with no enablers is its own chain", deep(causalChain(0), [0]));

  SUMMARY = CARDS[2]; RUN = 9;
  check("a card shows the run on show", deep(causalNodes(), [2]));
  check("its count is every edge touching it", causalCount() === 3);
  RUN = null;
  check("a card with no run picked shows all of its runs", deep(causalNodes(), [2]));

  SUMMARY = CARDS[0];
  check("the input card has no causes", causalNodes().length === 0);
  SUMMARY = { lane: "mir", runs: [3] };
  check("the machine lane has no causes", causalNodes().length === 0);

  check("a run resolves to the card holding it", cardForRun(8, "CPass").id === 3);
  check("a run no card holds falls back to its pass", cardForRun(12, "DPass").id === 4);
  check("the input card is never a jump target", cardForRun(0, "Nope") === null);

  SUMMARY = CARDS[2]; RUN = 9;
  const html = causesBodyHtml();
  check("the chain is drawn", html.includes("cchain") && html.includes("B"));
  check("an indirect enabler is marked", html.includes(">via<"));
  check("a pre-empted run is listed", html.includes("pre-empts") && html.includes('data-crun="12"'));

  if (failures.length) { console.log(failures.join("\n")); process.exit(1); }
  console.log("ok");
});
"""


def test_causality_viewer(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    proc = subprocess.run(["node", str(harness), str(FRONTEND / "app.js")],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "ok"
