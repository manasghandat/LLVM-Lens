"""Pipeline flow view: collapse, serpentine layout, and the graph it mounts.

The pure layer is driven through Node, like the other frontend checks, so the
whole risk surface is verifiable without a browser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run_node(harness: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", "-e", harness, *args],
        capture_output=True, text=True, timeout=60,
    )


# Two lanes: ir {input, change, two non-changes, change}, mir {input, change x2}.
# ids are globally unique and runIndex resets per lane, as the real report does.
FIXTURE = """
const M = { passes: [
  { id: 1, lane: "ir", runIndex: 0, name: "Input IR", isInput: true, changed: true },
  { id: 2, lane: "ir", runIndex: 1, name: "mem2reg", changed: true, timeMs: 0.4,
    lineDelta: { added: 5, removed: 2 }, analysisCounts: { run: 2, invalidated: 1 } },
  { id: 3, lane: "ir", runIndex: 2, name: "noop-a", changed: false, timeMs: 0.5,
    lineDelta: { added: 0, removed: 0 } },
  { id: 4, lane: "ir", runIndex: 3, name: "noop-b", changed: false, timeMs: 0.5,
    lineDelta: { added: 0, removed: 0 } },
  { id: 5, lane: "ir", runIndex: 4, name: "gvn", changed: true, timeMs: 1.1,
    lineDelta: { added: 1, removed: 1 } },
  { id: 6, lane: "mir", runIndex: 0, name: "Optimized IR", isInput: true, changed: true },
  { id: 7, lane: "mir", runIndex: 1, name: "regalloc", changed: true, timeMs: 0,
    spillCount: 3 },
  { id: 8, lane: "mir", runIndex: 2, name: "prologepilog", changed: true, timeMs: 0 },
] };
const OPTS = (o) => Object.assign({ lane: "both", onlyChanged: false, cols: 10,
  charW: 6, colStride: 250, expanded: new Set() }, o);
"""

PIPE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("/* --- CFG");
const end = src.indexOf("/* --- boot");
if (start < 0 || end < 0) { console.error("pipeline section not found"); process.exit(2); }
const escapeHtml = (s) => String(s);
eval(src.slice(start, end));

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
__FIXTURE__

// --- collapse ---------------------------------------------------------------
const ir = pipeCollapse(M.passes, "ir", false);
check("a run of two non-changes collapses to one span",
      ir.nodes.filter(n => n.kind === "agg").length === 1);
const span = ir.nodes.find(n => n.kind === "agg");
check("the span counts its members", span.count === 2);
check("the span is keyed by runIndex bounds, not array position", span.key === "air:2-3");
check("the span sums its members' time", Math.abs(span.timeMs - 1.0) < 1e-9);
check("the span carries its member ids in order",
      JSON.stringify(span.ids) === JSON.stringify([3, 4]));
check("the span is expandable when not filtering",
      span.expandable === true);
check("input cards never enter a span",
      ir.nodes.filter(n => n.kind === "agg").every(s => !s.ids.includes(1)));
check("a change keeps its own node",
      ir.nodes.some(n => n.summaryId === 2 && n.kind === "pass"));

// A lone non-change between two changes is a plain dimmed node, not a
// one-child span -- expanding it would be a dead interaction.
const lone = pipeCollapse([
  { id: 10, lane: "ir", runIndex: 1, name: "a", changed: true },
  { id: 11, lane: "ir", runIndex: 2, name: "b", changed: false },
  { id: 12, lane: "ir", runIndex: 3, name: "c", changed: true },
], "ir", false);
check("a lone non-change is a plain node", lone.nodes.length === 3);
check("...of kind noop", lone.nodes[1].kind === "noop");
check("...keeping its real name", lone.nodes[1].name === "b");
check("...and no span was produced", lone.nodes.every(n => n.kind !== "agg"));

// A custom pass must break a run, or it would vanish from the view while every
// other surface still shows it.
const custom = pipeCollapse([
  { id: 20, lane: "ir", runIndex: 1, name: "p1", changed: false },
  { id: 21, lane: "ir", runIndex: 2, name: "my-pass", changed: false, isCustom: true },
  { id: 22, lane: "ir", runIndex: 3, name: "p3", changed: false },
], "ir", false);
check("a custom pass keeps its own node",
      custom.nodes.some(n => n.summaryId === 21 && n.kind === "pass"));
check("...and splits the run around it", custom.nodes.length === 3);

// null times are skipped, not treated as zero.
const nulls = pipeCollapse([
  { id: 30, lane: "ir", runIndex: 1, name: "p1", changed: false },
  { id: 31, lane: "ir", runIndex: 2, name: "p2", changed: false },
], "ir", false);
check("an all-null run has no time at all", nulls.nodes[0].timeMs === null);
const mixed = pipeCollapse([
  { id: 32, lane: "ir", runIndex: 1, name: "p1", changed: false, timeMs: null },
  { id: 33, lane: "ir", runIndex: 2, name: "p2", changed: false, timeMs: 2 },
], "ir", false);
check("a partly-timed run sums only what it has", mixed.nodes[0].timeMs === 2);

// Spills and analyses are summed onto the span so the warning is not lost.
const sumspan = pipeCollapse([
  { id: 40, lane: "mir", runIndex: 1, name: "p1", changed: false, spillCount: 2,
    analysisCounts: { run: 3, invalidated: 1 } },
  { id: 41, lane: "mir", runIndex: 2, name: "p2", changed: false, spillCount: 5,
    analysisCounts: { run: 1, invalidated: 4 } },
], "mir", false);
check("span sums spills", sumspan.nodes[0].spills === 7);
check("span sums analyses run", sumspan.nodes[0].run === 4);
check("span sums analyses invalidated", sumspan.nodes[0].invalidated === 5);

// "changed only" already means runs are collapsed, so a lone non-change goes
// away and a span stops being expandable.
const oc = pipeCollapse(M.passes, "ir", true);
check("filtering drops a lone non-change",
      oc.nodes.every(n => n.kind !== "noop"));
const ocSpan = oc.nodes.find(n => n.kind === "agg");
check("filtering keeps a real span", !!ocSpan);
check("...but stops it expanding", ocSpan.expandable === false);

// The lane sequence is runIndex order, NOT pipelineTree order. The ir tree is
// scope-grouped, so drawing it would render a wrong pass order.
const scrambled = [
  { id: 53, lane: "ir", runIndex: 3, name: "c", changed: true },
  { id: 51, lane: "ir", runIndex: 1, name: "a", changed: true },
  { id: 52, lane: "ir", runIndex: 2, name: "b", changed: true },
];
const seqIds = pipeCollapse(scrambled, "ir", false).nodes.map(n => n.summaryId);
check("passes are ordered by runIndex whatever order they arrive in",
      JSON.stringify(seqIds) === JSON.stringify([51, 52, 53]));

// --- row rule ---------------------------------------------------------------
check("the first row runs right",
      JSON.stringify(pipeRowRule(10, null)) === JSON.stringify({ startSlot: 0, step: 1 }));
check("a full second row runs left",
      JSON.stringify(pipeRowRule(10, 9)) === JSON.stringify({ startSlot: 9, step: -1 }));
// The naive parity rule would put these at slots 9,10,11 -- off the grid.
check("a short row wraps under the previous tail",
      JSON.stringify(pipeRowRule(3, 9)) === JSON.stringify({ startSlot: 9, step: -1 }));
check("a row that fits still runs right",
      JSON.stringify(pipeRowRule(5, 2)) === JSON.stringify({ startSlot: 2, step: 1 }));

// --- layout -----------------------------------------------------------------
const spec = pipeGraphSpec(M, OPTS({}));
check("every step became a node", Object.keys(spec.data).length === 7);
check("steps are chained one edge per gap", spec.edges.length === 6);
check("all positions are finite",
      Object.values(spec.positions).every(p => isFinite(p.x) && isFinite(p.y)));
check("row tags are positioned too",
      spec.tags.length === spec.rows.length
      && spec.tags.every(t => spec.positions[t.key]));
check("the stage has a real size", spec.width > 0 && spec.height > 0);

// No two boxes may overlap. With 12px labels and 210px cells this is the failure
// that would make the view unreadable again, and it is invisible in the spec.
const boxes = Object.entries(spec.data).map(([k, d]) => ({ k, x: spec.positions[k].x, y: spec.positions[k].y,
  w: d.w, h: d.h }));
let collisions = [];
for (let i = 0; i < boxes.length; i++) {
  for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (Math.abs(a.x - b.x) < (a.w + b.w) / 2 && Math.abs(a.y - b.y) < (a.h + b.h) / 2) {
      collisions.push(a.k + "/" + b.k);
    }
  }
}
check("no two boxes overlap", collisions.length === 0, collisions.slice(0, 4).join(" "));
// The gap has to be a real gap, not merely a non-overlap: adjacent boxes must
// clear each other by at least their own width.
const row0 = boxes.filter(b => b.y === Math.min(...boxes.map(c => c.y)));
const ordered = row0.slice().sort((p, q) => p.x - q.x);
const gaps = ordered.slice(1).map((b, i) => b.x - ordered[i].x - (b.w + ordered[i].w) / 2);
check("boxes in a row are separated, not merely not overlapping",
      gaps.length > 0 && gaps.every(g => g > 0),
      "min clearance " + Math.min(...gaps));
check("...by a margin worth calling spacing",
      gaps.every(g => g >= 20), "min clearance " + Math.min(...gaps));

// The fixture's names are short, so every box here sits on the min-width floor
// and never exercises the cap -- which is exactly where a squeezed column gap
// would start to hurt. Real pass names are long, so pin that path too.
const LONG = { passes: [
  { id: 90, lane: "ir", runIndex: 1, name: "loop-reduce-with-a-fairly-long-name",
    changed: true, timeMs: 0.4 },
  { id: 91, lane: "ir", runIndex: 2, name: "another-extremely-long-pass-name-x",
    changed: true, timeMs: 12.5 },
  { id: 92, lane: "ir", runIndex: 3, name: "third", changed: true, timeMs: 0 },
] };
const WIDE_PANE = 1127;   // what the probe measures for a normal window
const wideGrid = pipeGrid(WIDE_PANE);
const wide = pipeGraphSpec(LONG, OPTS({
  cols: wideGrid.cols, colStride: wideGrid.stride, maxW: wideGrid.maxW }));
const wboxes = Object.entries(wide.data)
  .map(([k, d]) => ({ x: wide.positions[k].x, w: d.w }))
  .sort((p, q) => p.x - q.x);
const wclear = wboxes.slice(1).map((b, i) => b.x - wboxes[i].x - (b.w + wboxes[i].w) / 2);
const wmax = Math.max(...wboxes.map(b => b.w));
check("a long label fills the cell it was given", wmax > wideGrid.maxW,
      "max w " + wmax + " vs label " + wideGrid.maxW);
check("...and the widest box still clears its neighbour",
      wclear.every(g => g >= 20), "min clearance " + Math.min(...wclear));

// labelBox hard-wraps without an ellipsis and a cell keeps two lines, so an
// unclipped name spills onto line 2 and pushes the time off the box: a pass
// would render as having no time purely because its name was long.
const llines = Object.values(wide.data).map(d => d.label.split("\n"));
check("no cell label runs past two lines", llines.every(l => l.length <= 2),
      "max " + Math.max(...llines.map(l => l.length)) + " lines");
check("...so every cell keeps a second line", llines.every(l => l[1]),
      JSON.stringify(llines.filter(l => !l[1])));
check("a long name is clipped, not wrapped",
      llines.some(l => /…$/.test(l[0])),
      JSON.stringify(llines.map(l => l[0])));
check("...and the time survives on line 2",
      llines.every(l => /ms|\u2014/.test(l[1])),
      JSON.stringify(llines.map(l => l[1])));
check("...so a clipped box still reads its dash rather than 0.00 ms",
      llines.some(l => /—/.test(l[1]))
      && !llines.some(l => /0\.00 ms/.test(l[1])),
      JSON.stringify(llines.map(l => l[1])));
check("...while a measured one keeps its number",
      llines.some(l => /12\.50 ms/.test(l[1])),
      JSON.stringify(llines.map(l => l[1])));

// --- grid -------------------------------------------------------------------
// The cell is sized to the pane so the target column count always fits. A fixed
// cell width would push the fifth column off the pane, reachable only by panning.
check("the target is five cells per row", pipeGrid(WIDE_PANE).cols === 5,
      "got " + pipeGrid(WIDE_PANE).cols);
check("...and a full row fits inside the pane it was sized for",
      wideGrid.cols * wideGrid.stride - wideGrid.gap <= WIDE_PANE,
      "row " + (wideGrid.cols * wideGrid.stride - wideGrid.gap) + " in " + WIDE_PANE);
check("the pitch leaves the column gap after a cell",
      wideGrid.stride - wideGrid.cellW === wideGrid.gap,
      "gap " + (wideGrid.stride - wideGrid.cellW));
check("...the same gap at every width",
      pipeGrid(760).stride - pipeGrid(760).cellW === wideGrid.gap);
// A roomier pane must not quietly buy a sixth column: five is the contract.
check("a wide pane still gives five", pipeGrid(1920).cols === 5);
// Narrowing gives up columns rather than shrinking cells past legibility.
const narrow = pipeGrid(760);
check("a narrow pane drops a column", narrow.cols < 5, "cols " + narrow.cols);
check("...rather than squeezing the cells", narrow.cellW >= narrow.floor,
      "cell " + narrow.cellW + " floor " + narrow.floor);
check("...and still fits", narrow.cols * narrow.stride - narrow.gap <= 760);
// A degenerate pane must not produce NaN geometry.
check("a zero-width pane is survivable",
      isFinite(pipeGrid(0).cellW) && pipeGrid(0).cols >= 1 && pipeGrid(0).maxW > 0,
      JSON.stringify(pipeGrid(0)));

// The label width is what the caller says, not a constant baked in here.
const capped = pipeGraphSpec(LONG, OPTS({ cols: 3, maxW: 120 }));
const cappedMax = Math.max(...Object.values(capped.data).map(d => d.w));
const defaultMax = Math.max(...Object.values(pipeGraphSpec(LONG, OPTS({})).data).map(d => d.w));
check("a narrow maxW narrows the boxes", cappedMax < defaultMax,
      "capped " + cappedMax + " vs default " + defaultMax);
// box = label width + a 11px pad each side, so 120 + 22 is the ceiling.
check("...to that width plus its padding", cappedMax <= 142, "max w " + cappedMax);
check("the first row heads right", spec.tags[0].dir === 1);

// Reading order is recoverable from the ordinals alone, whatever the geometry.
check("every node carries its ordinal",
      Object.values(spec.data).every(d => /^#\d{3}/m.test(d.label)
        || /^#\d{3}–#\d{3}/m.test(d.label)));
check("a span shows a range and a count",
      /2 passes/.test(spec.data["air:2-3"].label));

// The both-lane chain has exactly one crossing edge, and it joins the last opt
// step to the machine lane's entry card (ids are global; runIndex is not).
const handoff = spec.edges.filter(e => e.cls === "handoff");
check("exactly one handoff edge", handoff.length === 1);
check("the handoff leaves the last opt step", handoff[0].source === "p5");
check("...and lands on the machine entry card", handoff[0].target === "p6");
check("no other edge is marked handoff",
      spec.edges.every(e => e.cls === "handoff" || e.source !== "p5" || e.target !== "p6"));

// One lane alone has no crossing.
const solo = pipeGraphSpec(M, OPTS({ lane: "mir" }));
check("a single lane has no handoff",
      solo.edges.every(e => e.cls !== "handoff"));
check("a single lane draws only its own steps", Object.keys(solo.data).length === 3);
check("a single lane does not dim anything",
      solo.stats.lanes.length === 1);

// --- wraps ------------------------------------------------------------------
// A row boundary is where the chain hops to the next row. Those edges have to be
// classed, or the view draws the hop as an ordinary arrow between far-apart rows.
const wrapped = pipeGraphSpec(M, OPTS({ cols: 3 }));
check("a narrow grid produces rows", wrapped.rows.length === 3);
check("wraps are the row boundaries",
      wrapped.wrapAfter.length === wrapped.rows.length - 1);
check("...one edge per boundary",
      wrapped.edges.filter(e => e.cls === "wrap").length === wrapped.rows.length - 1);
// A boundary is either a wrap or the lane handoff, whichever it is.
check("every boundary edge is classed",
      wrapped.wrapAfter.every(i => wrapped.edges[i].cls === "wrap"
        || wrapped.edges[i].cls === "handoff"));
check("no wrap sits off a boundary",
      wrapped.edges.every((e, i) => e.cls !== "wrap" || wrapped.wrapAfter.includes(i)));
// Rows begin under the previous row's tail, so a wrap is a vertical hop.
const wrapXs = wrapped.edges.filter(e => e.cls === "wrap")
  .map(e => [wrapped.positions[e.source].x, wrapped.positions[e.target].x]);
check("every wrap is vertical",
      wrapXs.length > 0 && wrapXs.every(([x1, x2]) => Math.abs(x1 - x2) < 1e-9));
// A short final row must still contribute exactly one boundary.
check("the last row adds no trailing wrap",
      !wrapped.wrapAfter.includes(wrapped.order.length - 1));
check("one row wraps nowhere",
      spec.wrapAfter.length === 0 && spec.edges.every(e => e.cls !== "wrap"));

// A wrap is where the row ran out, not something happening in the pipeline, so
// it must not wear the accent that marks a real one. Fifteen of them at the
// handoff's blue drowned out the single edge that means anything.
const rules = pipeStyle();
const rule = sel => rules.find(r => r.selector === sel).style;
const plainEdge = rule("edge"), wrapEdge = rule("edge.wrap"), handEdge = rule("edge.handoff");
check("a row wrap is drawn in the ordinary edge colour",
      wrapEdge["line-color"] === plainEdge["line-color"]
      && wrapEdge["target-arrow-color"] === plainEdge["target-arrow-color"],
      wrapEdge["line-color"] + " vs " + plainEdge["line-color"]);
check("...leaving the accent to the lane handoff alone",
      handEdge["line-color"] !== plainEdge["line-color"],
      handEdge["line-color"]);
check("...and still dashed, so it reads as a continuation rather than a join",
      wrapEdge["line-style"] === "dashed" && plainEdge["line-style"] !== "dashed");

// --- legend -----------------------------------------------------------------
// The key has to describe the picture that is actually drawn: every colour it
// shows must be one the styles paint, or it is teaching the wrong graph.
const legend = pipeLegend();
// The churn and time bars are inline SVGs carried on the node data, not style
// rules, so the scan covers a built spec's own strings as well as the styles.
const painted = new Set();
const scan = v => {
  if (typeof v !== "string") return;
  let t = v;
  try { t = decodeURIComponent(v); } catch (e) { /* a literal, not a data URI */ }
  (t.match(/#[0-9a-fA-F]{6}\b/g) || []).forEach(x => painted.add(x));
};
rules.forEach(r => Object.values(r.style || {}).forEach(scan));
Object.values(pipeGraphSpec(M, OPTS({})).data)
  .forEach(d => Object.values(d).forEach(scan));
const shown = [...legend.matchAll(
  /(?:border-color|background-color|border-top-color):\s*([^;"]+)/g)].map(m => m[1]);
check("the legend actually draws swatches", shown.length >= 8, shown.length + " colours");
check("every colour it shows is one the graph paints",
      shown.every(x => painted.has(x)),
      JSON.stringify(shown.filter(x => !painted.has(x))));
// Each mark the graph can draw has to be named, or the key is incomplete.
const named = ["changed", "unchanged", "collapsed span", "span member", "entry",
               "custom", "selected", "next pass", "row break", "opt → llc",
               "removed", "added", "share of time", "ordinal", "churn", "0.1 ms"];
check("the legend names every mark the graph draws",
      named.every(n => legend.includes(n)),
      JSON.stringify(named.filter(n => !legend.includes(n))));
// Scoped to one keyed entry, so a colour borrowed from a neighbour cannot
// stand in for the mark being checked.
const item = label => {
  const at = legend.indexOf("<em>" + label + "</em>");
  return at < 0 ? "" : legend.slice(legend.lastIndexOf("<span class=\"pl-it\">", at), at);
};
const lineCol = label => (item(label).match(/border-top-color:([^;"]+)/) || [])[1];
// The row break wears the ordinary edge colour now, so the key must say so
// rather than lending it the accent the reader is meant to read as a handoff.
check("the row break is keyed in the plain colour it is drawn in",
      lineCol("row break") === wrapEdge["line-color"], lineCol("row break"));
check("...as is the handoff in its own, which is not that colour",
      lineCol("opt → llc") === handEdge["line-color"]
      && lineCol("opt → llc") !== wrapEdge["line-color"], lineCol("opt → llc"));
check("...and the two are told apart by the dash as well",
      /dashed/.test(item("row break")) && /solid/.test(item("next pass")),
      item("row break") + " | " + item("next pass"));
check("the changed and unchanged keys wear their own borders",
      item("changed").includes("border-color:" + rule("node")["border-color"])
      && item("unchanged").includes("border-color:" + rule("node.noop")["border-color"]),
      item("changed") + " | " + item("unchanged"));
check("the span and its members are told apart by the dash, as in the graph",
      /dashed/.test(item("collapsed span")) && !/dashed/.test(item("span member")),
      item("collapsed span") + " | " + item("span member"));
// The three accent overrides: each key must wear the border the graph paints,
// or the reader is being taught a colour that appears nowhere.
[["entry", "node.entry"], ["custom", "node.custom"], ["isel · selected", "node.isel"]]
  .forEach(([label, sel]) => check("the " + label + " key wears its painted border",
    item(label).includes("border-color:" + rule(sel)["border-color"]),
    label + " " + item(label)));
// Both bars are measured against a whole, so the key has to show that whole:
// track first, then a shorter fill. A fill spanning its track would read as
// "all of the time", which no cell can mean.
const barGeom = label => {
  const w = (item(label).match(/width:(\d+)px/g) || []).map(s => +s.match(/\d+/)[0]);
  return { track: w[0], fill: w.slice(1).reduce((a, b) => a + b, 0), segs: w.length - 1 };
};
[["share of time", "time bar", 1], ["lines removed / added", "churn bar", 2]]
  .forEach(([label, what, segs]) => {
    const g = barGeom(label);
    check("the " + what + " is a fill on a longer track, so it reads as a share",
          g.segs === segs && g.fill > 0 && g.fill < g.track, JSON.stringify(g));
    // The track is what makes the fill a share, so it has to be drawn, and in
    // a colour the graph itself uses rather than one invented for the key.
    check("...over a track drawn in the graph's own recessive fill",
          (item(label).match(/background-color:([^;"]+)/) || [])[1]
          === rule("node.agg")["background-color"],
          (item(label).match(/background-color:([^;"]+)/) || [])[1]);
  });
// The two bars key the two things a cell measures, so they belong side by
// side with the rest of the marks, churn first.
const diffAt = legend.indexOf("<em>lines removed / added</em>");
const timeAt = legend.indexOf("<em>share of time</em>");
check("the two bars are keyed side by side, churn then time",
      diffAt > 0 && timeAt > diffAt
      && legend.slice(diffAt, timeAt).indexOf('<span class="pl-it"') >= 0,
      JSON.stringify([diffAt, timeAt]));
check("...with no entry given a placement of its own",
      !legend.includes('<span class="pl-it '),
      legend.slice(legend.indexOf('<span class="pl-it '), 60));

// Each bar is drawn inside its cell, on the side its reading belongs to:
// churn on the left, time on the right.
check("the churn bar inside a pass cell is anchored to its left edge",
      rule("node")["background-position-x"] === "0px",
      JSON.stringify(rule("node")["background-position-x"]));
check("...while the time bar inside a span is anchored to its right",
      rule("node.agg")["background-position-x"] === "100%",
      JSON.stringify(rule("node.agg")["background-position-x"]));
check("...and a span's members inherit that, being time too",
      rule("node.childins")["background-position-x"] === "100%",
      JSON.stringify(rule("node.childins")["background-position-x"]));
// Resolved the way cytoscape would: an override that says nothing about y
// inherits the base, so this reads the effective value, not the raw rule.
const bottom = sel => {
  const v = rule(sel)["background-position-y"];
  return v === undefined ? rule("node")["background-position-y"] : v;
};
check("...both riding the bottom edge, under the label",
      ["node", "node.agg", "node.childins"].every(s => bottom(s) === "100%"),
      JSON.stringify(["node", "node.agg", "node.childins"].map(bottom)));
check("...at the width its own data gives it, so length still means weight",
      rule("node")["background-width"] === "data(bw)"
      && rule("node")["background-fit"] === "none",
      JSON.stringify([rule("node")["background-width"], rule("node")["background-fit"]]));

// Every consecutive pair must be joined -- this is the check that catches a
// dropped or duplicated edge.
check("each edge starts at the next cell in the chain",
      spec.edges.every((e, i) => e.source === spec.order[i]));

// An edge endpoint missing from itemKeys is never mounted, and cytoscape
// rejects the edge outright. That is exactly what expanding a span used to do:
// the children were in `data` but not in the set the mounter walked.
const exSpec = pipeGraphSpec(M, OPTS({ expanded: new Set(["air:2-3"]) }));
const unmounted = exSpec.edges.filter(e =>
  !exSpec.itemKeys.includes(e.source) || !exSpec.itemKeys.includes(e.target));
check("every edge endpoint is mounted, expanded or not", unmounted.length === 0);
check("expanded children are mounted elements",
      exSpec.itemKeys.includes("p3") && exSpec.itemKeys.includes("p4"));
check("...while the chain still steps cell to cell",
      exSpec.order.length === spec.order.length
      && exSpec.edges.length === spec.edges.length);
check("the mounted set is the data set",
      exSpec.itemKeys.length === Object.keys(exSpec.data).length
      && spec.itemKeys.length === Object.keys(spec.data).length);
check("collapsed, the chain and the mounted set agree",
      spec.itemKeys.length === spec.order.length);

// --- degenerate -------------------------------------------------------------
const empty = pipeGraphSpec({ passes: [] }, OPTS({}));
check("an empty manifest yields nothing, not a throw",
      Object.keys(empty.data).length === 0 && empty.edges.length === 0);
const one = pipeGraphSpec({ passes: [M.passes[1]] }, OPTS({ lane: "ir" }));
check("one pass draws one node and no edges",
      Object.keys(one.data).length === 1 && one.edges.length === 0);
check("...with a positive stage", one.width > 0 && one.height > 0);
check("...and no NaN position",
      Object.values(one.positions).every(p => isFinite(p.x) && isFinite(p.y)));

// A lane that never ran (opt crashed) must not fabricate a crossing.
const halfLane = pipeGraphSpec(M, OPTS({ lane: "ir" }));
check("a lane with no counterpart still renders", Object.keys(halfLane.data).length === 4);

// --- expansion --------------------------------------------------------------
const ex = pipeGraphSpec(M, OPTS({ expanded: new Set(["air:2-3"]) }));
check("expanding a span inlines its members",
      Object.keys(ex.data).length === Object.keys(spec.data).length + 2);
check("...as children of that span", !!ex.data["p3"] && !!ex.data["p4"]);
check("...growth is vertical only",
      Math.abs(ex.width - spec.width) < 1e-9 && ex.height > spec.height);
// The expand affordance lives in the detail panel, not the label. A box that
// looked like every other box but behaved differently was the usability bug.
check("a collapsed span offers to expand in its detail panel",
      /expand/.test(pipeDetailHtml(spec.raw["air:2-3"])));
check("an expanded span offers to collapse there instead",
      /collapse/.test(pipeDetailHtml(ex.raw["air:2-3"])));
check("...and that button carries the span's key",
      /data-pipe-key="air:2-3"/.test(pipeDetailHtml(ex.raw["air:2-3"])));
check("no label carries an affordance any more",
      Object.values(spec.data).every(d => !/expand|collapse/.test(d.label)));

// --- detail panel -----------------------------------------------------------
// The panel is where the metrics the box could not hold are read, so it has to
// carry every one of them, and nothing that is not there.
const spanDetail = pipeDetailHtml(spec.raw["air:2-3"]);
check("the span panel names the range", /#002/.test(spanDetail) && /#003/.test(spanDetail));
check("...and its pass count", /2 passes/.test(spanDetail));
// The header range and the member list must speak the same numbering. They once
// did not: members printed global pass ids, which read as a different range.
check("...and its members are numbered the same way as its range",
      /holds #002 #003/.test(spanDetail));
const changeDetail = pipeDetailHtml(spec.raw["p2"]);
check("a pass panel leads with its ordinal and name",
      /#001/.test(changeDetail) && /mem2reg/.test(changeDetail));
check("...reports its churn", /\+5/.test(changeDetail) && /−2/.test(changeDetail));
check("...and its analysis counts",
      /2 run · 1 invalid/.test(changeDetail));
check("...and has no expand button, only the two jumps",
      !/pipe-act="toggle"/.test(changeDetail)
      && /pipe-act="cfg"/.test(changeDetail) && /pipe-act="diff"/.test(changeDetail));
check("a span has no jump buttons, having no pass of its own",
      !/pipe-act="cfg"/.test(spanDetail));
// A filter flattens the spans, so an expand button there would be dead.
check("a span under “changed only” offers no expand",
      !/pipe-act="toggle"/.test(
        pipeDetailHtml(pipeGraphSpec(M, OPTS({ onlyChanged: true })).raw["air:2-3"])));
check("no node renders an empty panel", Object.values(spec.raw)
  .every(n => pipeDetailHtml(n).includes("pipe-detail-in")));
check("no panel leaks an undefined", Object.values(spec.raw)
  .every(n => !/undefined|NaN/.test(pipeDetailHtml(n))));
check("the unmeasured pass says so rather than 0.00 ms",
      /—/.test(pipeDetailHtml(spec.raw["p7"])) && /3/.test(pipeDetailHtml(spec.raw["p7"])));

// --- churn ------------------------------------------------------------------
// Nodes carry added/removed flat while a raw pass nests them under lineDelta, so
// reading the wrong shape yielded 0 for every node: churn was invisible in the
// label, absent from the panel, and every churn bar was a zero-width no-op.
check("a changed pass shows its churn in the label",
      /\+5 −2/.test(spec.data["p2"].label));
check("...and in its panel", /\+5 −2/.test(changeDetail));
check("the plumbing for the bars is not dead",
      parseFloat(spec.data["p2"].bw) > 0);
check("an unchanged pass claims no churn",
      !/\+/.test(spec.data["p7"].label) && /none/.test(pipeDetailHtml(spec.raw["p7"])));

// --- honest timing ----------------------------------------------------------
check("a measured time is shown", /0\.40 ms/.test(spec.data["p2"].label));
// 0.00 ms is the -time-passes resolution floor, not a measurement, so a machine
// pass that ran faster than the tool can resolve must not read as 0.00 ms. The
// cell has no room for a word, so it takes the same dash as an untimed pass and
// the note above the graph carries the explanation.
check("a zero time reads as a dash, not as 0.00 ms",
      !/0\.00 ms/.test(spec.data["p7"].label) && /—/.test(spec.data["p7"].label));
// The narrowest box the grid ever makes is its 150px floor, and line 2 there
// holds 20 characters. A dash plus one of three short tails fits; a churn at
// the format's upper bound does not, and line 2 has to clip rather than wrap,
// still leading with the time -- or a narrow box reads as churn with no time.
const NARROW = { passes: [
  { id: 95, lane: "ir", runIndex: 1, name: "gvn", changed: true, timeMs: 0,
    lineDelta: { added: 12345678, removed: 87654321 } },
] };
const nline = pipeGraphSpec(NARROW, OPTS({ cols: 3, maxW: 128 })).data.p95.label.split("\n");
check("a line 2 too long for the box does clip", nline[1].length === 20,
      JSON.stringify(nline));
check("...and still leads with the time", /^—/.test(nline[1]), JSON.stringify(nline));
// Both lines are clipped to the cell's budget before labelBox sees them, so it
// can never wrap: a third line would render outside the 58px box a cell holds.
check("...and the box is still exactly two lines", nline.length === 2,
      JSON.stringify(nline));
// A dash and a short churn are what cells actually carry, so that must not clip.
const short2 = pipeGraphSpec(NARROW, OPTS({ cols: 3, maxW: 128, })).raw["p95"];
check("...while an ordinary line 2 is left whole",
      !/…/.test(pipeNodeLabel({ ...short2, added: 302, removed: 287 }, 6, 128)
        .split("\n")[1]));
// The box a cell ends up with is the box its label was clipped for, so the two
// cannot disagree: a narrower pane must clip harder, never wrap.
const tighter = pipeGraphSpec(LONG, OPTS({ cols: 3, maxW: 90 }));
const tlines = Object.values(tighter.data).map(d => d.label.split("\n"));
check("a narrow box still holds exactly two lines",
      tlines.every(l => l.length === 2), JSON.stringify(tlines));
check("...and clips harder than the wide one",
      Math.max(...tlines.map(l => l[0].length))
      < Math.max(...llines.map(l => l[0].length)),
      "narrow " + Math.max(...tlines.map(l => l[0].length))
      + " vs wide " + Math.max(...llines.map(l => l[0].length)));
check("...keeping the time on line 2", tlines.every(l => /ms|\u2014/.test(l[1])),
      JSON.stringify(tlines.map(l => l[1])));
check("the machine lane reports a low timed fraction",
      spec.stats.timedFraction === 0);
check("the opt lane reports a full one",
      pipeCollapse(M.passes, "ir", false).stats.timedFraction === 1);

if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("frontend pipeline checks passed");
"""
PIPE_HARNESS = PIPE_HARNESS.replace("__FIXTURE__", FIXTURE)


PIPE_CYTOSCAPE_HARNESS = r"""
const fs = require("fs");
const path = require("path");
const vendor = process.argv[1];

const warnings = [];
const origWarn = console.warn;
console.warn = (...a) => warnings.push(a.join(" "));

const Module = require("module");
const orig = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "dagre") return path.join(vendor, "dagre.min.js");
  return orig.call(this, request, ...rest);
};

const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("/* --- CFG");
const end = src.indexOf("/* --- boot");
const escapeHtml = (s) => String(s);
eval(src.slice(start, end));

const cytoscape = require(path.join(vendor, "cytoscape.min.js"));
require(path.join(vendor, "cytoscape-dagre.js"))(cytoscape);

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
__FIXTURE__

// Mirrors mountPipe's element build: walk itemKeys, not the cell chain.
const mount = spec => {
  const elements = [];
  for (const key of spec.itemKeys) {
    const d = spec.data[key];
    if (d) elements.push({ data: d, classes: spec.classes[key] });
  }
  for (const e of spec.edges) {
    elements.push({ data: { id: e.id, source: e.source, target: e.target }, classes: e.cls });
  }
  for (const t of spec.tags) {
    elements.push({ data: { id: t.key, label: t.label }, classes: "rowtag" });
  }
  return cytoscape({ headless: true, styleEnabled: true, style: pipeStyle(), elements });
};

const spec = pipeGraphSpec(M, OPTS({}));
const cy = mount(spec);
cy.layout({ name: "preset", fit: false, positions: spec.positions }).run();

check("node count survives the round trip",
      cy.nodes().not(".rowtag").length === Object.keys(spec.data).length);
check("edge count survives the round trip", cy.edges().length === spec.edges.length);
check("row tags are not nodes of the pipeline",
      cy.nodes(".rowtag").length === spec.tags.length);

// Positions are ours, so they must come back exactly.
const drift = Object.entries(spec.positions).filter(([k, p]) => {
  const n = cy.$id(k);
  return !n.empty() && (Math.abs(n.position("x") - p.x) > 1e-6
    || Math.abs(n.position("y") - p.y) > 1e-6);
});
check("preset positions round-trip unchanged", drift.length === 0);

// Chain completeness: every consecutive step is joined.
const hasEdge = (a, b) => cy.edges().some(e => e.data("source") === a && e.data("target") === b);
const cells = spec.order;
const gaps = [];
for (let i = 0; i + 1 < cells.length; i++) {
  const a = cells[i], b = cells[i + 1];
  if (!hasEdge(a, b)) gaps.push(a + "->" + b);
}
check("every consecutive pair is joined: " + gaps.join(", "), gaps.length === 0);

// The handoff edge must carry its label without cytoscape complaining about a
// mapping on edges that have no such field.
check("the handoff edge is styled", cy.$id(spec.edges.find(e => e.cls === "handoff").id)
      .hasClass("handoff"));
check("the handoff label renders", !!/opt/.test(
      cy.$id(spec.edges.find(e => e.cls === "handoff").id).style("label")));
check("no styling warnings", warnings.length === 0);

// A span carries a bar; a node without churn does not.
check("a span draws a time-share bar",
      /^data:image\/svg\+xml/.test(cy.$id("air:2-3").style("background-image")));
check("a changed pass draws a churn bar",
      /^data:image\/svg\+xml/.test(cy.$id("p2").style("background-image")));
check("a bar-less node reports zero width", cy.$id("p1").style("background-width") === "0px");

check("row tags never intercept a tap", cy.$id("t0").style("events") === "no");

// Expanding a span remounts the whole graph. Its children are real edge
// endpoints, and cytoscape throws on an edge whose endpoint was never added --
// walking the cell chain instead of the mounted set crashed here.
const exSpec = pipeGraphSpec(M, OPTS({ expanded: new Set(["air:2-3"]) }));
const exCy = mount(exSpec);
exCy.layout({ name: "preset", fit: false, positions: exSpec.positions }).run();
check("an expanded span mounts", exCy.nodes().not(".rowtag").length
      === Object.keys(exSpec.data).length);
check("...with its children mounted",
      exCy.$id("p3").nonempty() && exCy.$id("p4").nonempty());
check("...and every edge endpoint present",
      exCy.edges().every(e => exCy.$id(e.data("source")).nonempty()
        && exCy.$id(e.data("target")).nonempty()));
check("...with no edge dropped", exCy.edges().length === exSpec.edges.length);
exCy.destroy();

cy.destroy();
console.warn = origWarn;
if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("headless cytoscape pipeline checks passed");
"""
PIPE_CYTOSCAPE_HARNESS = PIPE_CYTOSCAPE_HARNESS.replace("__FIXTURE__", FIXTURE)


def test_pipeline_collapse_and_layout():
    result = _run_node(PIPE_HARNESS, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_pipeline_graph_mounts_headlessly():
    result = _run_node(
        PIPE_CYTOSCAPE_HARNESS, str(FRONTEND / "vendor"), str(FRONTEND / "app.js")
    )
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_pipeline_view_is_wired_into_every_view_registry():
    """The view needs four separate registrations; missing one half-wires it."""
    html = (FRONTEND / "index.html").read_text()
    app = (FRONTEND / "app.js").read_text()

    assert 'data-mode="pipeline"' in html, "no chip in the mode row"
    assert '"structure", "analyses", "pipeline"' in app, "not a global view"
    assert 'mode === "pipeline" ? pipePaneHtml()' in app, "not in the render dispatch"
    assert "mountPipeGraphs();" in app, "never mounted after render"
    assert 'STATE.mode === "pipeline"' in app, "no context line"
    assert 'STATE.mode === "structure" || STATE.mode === "pipeline"' in app, (
        "the detail rows would not drill in"
    )


def test_pipeline_is_available_on_an_input_card():
    """It is a whole-report view, so a synthetic input card must still show it."""
    harness = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("// --- input cards (report.build_input_pass) ---");
const end = src.indexOf("function fnNames()");
let SUMMARY = { id: 83, lane: "mir", name: "Optimized IR", isInput: true, runIndex: 0 };
const STATE = { mode: "diff" };
const currentPassSummary = () => SUMMARY;
eval(src.slice(start, end));
if (!modeAvailable("pipeline")) { console.error("pipeline unavailable on input card"); process.exit(1); }
console.log("passed");
"""
    result = _run_node(harness, str(FRONTEND / "app.js"))
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
