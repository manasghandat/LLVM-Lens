/* LLVM-Lens report viewer. Hash-routed, zero dependencies, file://-safe.
 * Routes:  #/            pipeline (both lanes)
 *          #/pass/<id>   pass detail (diff / analyses / log / cfg / regmap / asm)
 *
 * Data: fetch('data/manifest.json') first; browsers block fetch() on
 * file:// URLs, so we fall back to the sibling .js wrappers emitted by
 * cli/emit.py. */

"use strict";

const DATA_JS = "window.__LLVM_LENS_DATA__";

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

/* --- lazy data loading ------------------------------------------------- */

async function loadJSON(name) {
  try {
    const response = await fetch(`data/${name}.json`);
    if (response.ok) return await response.json();
    throw new Error("not ok");
  } catch {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = `data/${name}.js`;
      script.onload = () => {
        if (name === "manifest") resolve(window.__LLVM_LENS_MANIFEST__);
        else resolve(window.__LLVM_LENS_DATA__[name]);
      };
      script.onerror = () => reject(new Error(`failed to load ${name}`));
      document.head.appendChild(script);
    });
  }
}

const manifestPromise = loadJSON("manifest");
const chunkCache = new Map();
async function loadPass(id) {
  const name = `pass-${id}`;
  if (!chunkCache.has(name)) chunkCache.set(name, loadJSON(name));
  return chunkCache.get(name);
}

/* --- diff helpers -------------------------------------------------------- */

// Longest common subsequence; returns equality mask over before/after lines.
function lcsMask(before, after) {
  const n = before.length, m = after.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = before[i] === after[j]
        ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const mask = { before: new Array(n).fill(false), after: new Array(m).fill(false) };
  for (let i = 0, j = 0; i < n && j < m; ) {
    if (before[i] === after[j]) { mask.before[i] = mask.after[j] = true; i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) i++; else j++;
  }
  return mask;
}

function diffLines(before, after) {
  const mask = lcsMask(before.split("\n"), after.split("\n"));
  return {
    before: before.split("\n").map((t, i) => ({ text: t, keep: mask.before[i] })),
    after: after.split("\n").map((t, i) => ({ text: t, keep: mask.after[i] })),
  };
}

function paneHtml(entries, kind) {
  // kind is "del" for the before pane (removed/changed lines, red) and
  // "add" for the after pane (added/changed lines, green).
  return entries.map(e =>
    e.keep ? escapeHtml(e.text)
           : `<span class="${kind}">${escapeHtml(e.text)}</span>`
  ).join("\n");
}

/* --- views ---------------------------------------------------------------- */

function fnMatchesFilter(fn) {
  const q = (document.getElementById("fnFilter") || {}).value || "";
  return !q || fn.toLowerCase().includes(q.toLowerCase());
}

function cardHtml(p) {
  const badges = [];
  if (p.changed) badges.push('<span class="badge changed">changed</span>');
  if (p.lane === "mir" && p.spillCount) badges.push(`<span class="badge spill">⚠ ${p.spillCount} spills</span>`);
  const a = p.analysisCounts || {};
  badges.push(`<span class="badge analyses">+${a.run || 0} −${a.invalidated || 0}</span>`);
  const idx = String(p.runIndex).padStart(3, "0");
  return `
    <div class="card ${p.changed ? "changed" : "unchanged"}" data-id="${p.id}">
      <div class="row">
        <span class="idx">#${idx}</span>
        <span class="name">${escapeHtml(p.name)}</span>
        <span class="badges">${badges.join("")}</span>
        <span class="time">${p.timeMs != null ? p.timeMs.toFixed(2) + " ms" : ""}</span>
      </div>
    </div>`;
}

function renderPipeline(manifest) {
  const onlyChanged = document.getElementById("changedOnly").checked;
  const passes = manifest.passes.filter(p =>
    !onlyChanged || p.changed || p.lane === "mir"
  );
  const lane = l => passes.filter(p => p.lane === l)
    .map(cardHtml).join("");
  const laneTitle = (title, sub, count) => `
    <div class="lane-title"><h2>${title}</h2><span class="lane-sub">${sub}</span>
      <span class="lane-rule"></span><span class="count">${count}</span></div>`;
  document.getElementById("app").innerHTML = `
    <div class="lanes">
      <section class="lane">
        ${laneTitle("opt", "IR passes", passes.filter(p => p.lane === "ir").length)}
        <div class="lane-body">
          ${lane("ir") || '<div class="card unchanged"><div class="row">(no IR passes captured)</div></div>'}
        </div>
      </section>
      <section class="lane">
        ${laneTitle("llc", "machine passes", passes.filter(p => p.lane === "mir").length)}
        <div class="lane-body">
          ${lane("mir") || '<div class="card unchanged"><div class="row">(no machine passes captured)</div></div>'}
        </div>
      </section>
      <div class="arrow"><span class="arrow-rule"></span>
        <span>final IR → ISel</span><span class="arrow-rule"></span></div>
    </div>`;
  document.querySelectorAll(".card").forEach(card =>
    card.addEventListener("click", () => { location.hash = `#/pass/${card.dataset.id}`; }));
}

// Final (end-of-pipeline) CFG per lane/function, from the manifest metadata.
let CURRENT_MANIFEST = null;

async function renderPassDetail(manifest, id) {
  CURRENT_MANIFEST = manifest;
  const summary = manifest.passes.find(p => p.id === +id);
  if (!summary) { document.getElementById("app").innerHTML = "<p>unknown pass</p>"; return; }
  const data = await loadPass(id);
  const tabs = ["Diff", "Analyses", "Log"];
  if (data.lane === "mir") tabs.push("RegMap");
  tabs.push("CFG");
  if (data.lane === "mir" && data.asm) tabs.push("Asm");

  const header = `
    <a class="breadcrumb" href="#/">← pipeline</a>
    <h1>${escapeHtml(data.name)} <span class="idx">#${data.runIndex} · ${data.lane === "mir" ? "machine" : "IR"}</span></h1>
    <div class="tabs">${tabs.map(t => `<button data-tab="${t}">${t}</button>`).join("")}</div>
    <div id="tabbody"></div>`;
  document.getElementById("app").innerHTML = header;
  document.querySelectorAll(".tabs button").forEach(btn =>
    btn.addEventListener("click", () => switchTab(data, btn.dataset.tab)));
  switchTab(data, "Diff");
}

function switchTab(data, tab) {
  document.querySelectorAll(".tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tab));
  const body = document.getElementById("tabbody");
  destroyCfgGraphs();
  if (tab === "Diff") body.innerHTML = diffTabHtml(data);
  else if (tab === "Analyses") body.innerHTML = analysesTabHtml(data);
  else if (tab === "Log") body.innerHTML = `<pre class="raw">${escapeHtml(data.log || "(no attributed output)")}</pre>`;
  else if (tab === "RegMap") body.innerHTML = regMapTabHtml(data);
  else if (tab === "CFG") { body.innerHTML = cfgTabHtml(data); mountCfgGraphs(); }
  else if (tab === "Asm") body.innerHTML = `<pre class="raw">${escapeHtml(data.asm)}</pre>`;
}

function diffTabHtml(data) {
  const fns = Object.entries(data.functions)
    .filter(([fn]) => fnMatchesFilter(fn));
  if (!fns.length) return Object.keys(data.functions).length
    ? "<p>(no functions match the filter)</p>"
    : "<p>(this pass ran but changed no IR — nothing to diff)</p>";
  return fns.map(([fn, change]) => {
    if (!change.changed) return "";
    const d = diffLines(change.before, change.after);
    const del = d.before.filter(e => !e.keep).length;
    const add = d.after.filter(e => !e.keep).length;
    return `
      <details class="fn" open>
        <summary>${escapeHtml(fn)}<span class="fn-stat">−${del} +${add}</span></summary>
        <div class="diff-pair">
          <div class="diff-pane">
            <div class="head"><span>before</span><span class="count del">−${del}</span></div>
            <pre>${paneHtml(d.before, "del")}</pre>
          </div>
          <div class="diff-pane">
            <div class="head"><span>after</span><span class="count add">+${add}</span></div>
            <pre>${paneHtml(d.after, "add")}</pre>
          </div>
        </div>
      </details>`;
  }).join("");
}

function analysesTabHtml(data) {
  const a = data.analyses || {};
  const rows = [["run", a.run], ["cached", a.cached], ["invalidated", a.invalidated]];
  return rows.map(([label, list]) => `
    <h2>${label} (${(list || []).length})</h2>
    <pre class="raw">${escapeHtml((list || []).join("\n") || "(none)")}</pre>`).join("");
}

function regMapTabHtml(data) {
  const maps = Object.entries(data.regMap || {})
    .filter(([fn]) => fnMatchesFilter(fn));
  if (!maps.length) return "<p>(no vreg assignments captured)</p>";
  return maps.map(([fn, map]) => `
    <h2>${escapeHtml(fn)}</h2>
    <table class="grid">
      <tr><th>vreg</th><th>physreg / slot</th></tr>
      ${Object.entries(map).map(([v, p]) =>
        `<tr><td>%${escapeHtml(v)}</td><td>${escapeHtml(p)}</td></tr>`).join("")}
    </table>`).join("");
}

function cfgTabHtml(data) {
  CFG_PENDING = [];
  const fns = Object.entries(data.functions)
    .filter(([fn]) => fnMatchesFilter(fn));
  const finals = ((CURRENT_MANIFEST || {}).metadata || {}).finalCfg || {};
  const finalCfg = finals[data.lane] || {};
  const finalFns = Object.keys(finalCfg).filter(fnMatchesFilter);
  const parts = [];
  if (finalFns.length) {
    parts.push(`<h2>final CFG · end of ${data.lane === "mir" ? "machine" : "IR"} pipeline</h2>`);
    parts.push(finalFns.map(fn => cfgBlockHtml(fn, "final", finalCfg[fn], true)).join(""));
  }
  parts.push(fns.map(([fn, change]) => {
    const out = [];
    if (change.dotBefore) out.push(cfgBlockHtml(fn, "before", change.dotBefore));
    if (change.dotAfter) out.push(cfgBlockHtml(fn, "after", change.dotAfter));
    return out.join("");
  }).join(""));
  if (!parts.join("")) {
    return Object.keys(data.functions).length
      ? "<p>(no functions match the filter)</p>"
      : "<p>(no CFG data)</p>";
  }
  return parts.join("");
}

function cfgBlockHtml(fn, side, dot, open) {
  const idx = CFG_PENDING.length;
  CFG_PENDING.push(dot);
  const body = `
    <div class="cfg-cy" data-idx="${idx}">
      <div class="cfg-cy-frame">
        <i class="cb tl" aria-hidden="true"></i><i class="cb tr" aria-hidden="true"></i>
        <i class="cb bl" aria-hidden="true"></i><i class="cb br" aria-hidden="true"></i>
        <div class="cfg-cy-canvas"></div>
        <div class="cfg-cy-zoom">ZOOM ×1.00</div>
      </div>
      <div class="cfg-cy-detail"></div>
    </div>
    <details class="dot"><summary>raw DOT</summary><pre class="raw">${escapeHtml(dot)}</pre></details>`;
  return `<details class="fn" ${open || side === "after" ? "open" : ""}><summary>${escapeHtml(fn)} <span class="side">· ${side}</span></summary>${body}</details>`;
}

/* --- CFG graph rendering (cytoscape + dagre, vendored in vendor/) --------- */

// Per-tab state: DOT payloads indexed as cfgBlockHtml builds the HTML, and
// live cytoscape instances that get destroyed on navigation.
let CFG_PENDING = [];
const CFG_INSTANCES = new Set();

// Mirrors the style.css token system: --well canvas, --panel2 nodes,
// --line-strong borders, --ink labels, --entry entry block, --del back
// edges, --trace selection.
const CFG_COLORS = {
  node: "#1a2136", border: "#3b4a6b", entry: "#8fd6a4",
  text: "#c9d6ec", edge: "#54648c", back: "#e2959b", accent: "#79c9dc",
};

// Label metrics. The node font is monospace, so character width is uniform
// and we can size each node's box to its label exactly: lines wrap at
// CFG_FONT.maxW (emulating the old renderer's wrap) and the box grows with
// the number of visual lines.
const CFG_FONT = { size: 10, charW: 6.0, lineH: 14, padX: 10, padY: 8, maxW: 300 };

function labelBox(label, charW = CFG_FONT.charW) {
  const { lineH, padX, padY, maxW } = CFG_FONT;
  // Safety margin: cytoscape word-wraps (text-wrap: wrap) at maxW, so our
  // hard-wrapped lines must stay measurably narrower than that.
  const maxChars = Math.max(1, Math.floor((maxW - 4) / charW));
  const lines = String(label).split("\n");
  // Hard-wrap long lines so the rendered label matches the computed box.
  const wrapped = [];
  let visual = 0;
  for (const line of lines) {
    let n = Math.max(1, Math.ceil(line.length / maxChars));
    for (let i = 0; i < n; i++) wrapped.push(line.slice(i * maxChars, (i + 1) * maxChars));
    visual += n;
  }
  const contentW = Math.min(maxW, Math.max(1, ...lines.map(l => l.length)) * charW);
  return { w: contentW + 2 * padX, h: visual * lineH + 2 * padY, wrapped: wrapped.join("\n") };
}

function parseDot(dot) {
  const nodes = [], edges = [];
  for (const line of String(dot || "").split("\n")) {
    let m = line.match(/^\s*n(\d+) \[label="((?:[^"\\]|\\.)*)"(?:, code="((?:[^"\\]|\\.)*)")?\]\s*;?$/);
    if (m) {
      const un = (s) => s.replace(/\\n/g, "\n").replace(/\\(.)/g, "$1");
      nodes.push({ id: +m[1], label: un(m[2]), code: m[3] !== undefined ? un(m[3]) : "" });
      continue;
    }
    m = line.match(/^\s*n(\d+) -> n(\d+);$/);
    if (m) edges.push([+m[1], +m[2]]);
  }
  return { nodes, edges };
}

function mountCfgGraphs() {
  document.querySelectorAll("#tabbody .cfg-cy").forEach(el => {
    mountCfg(el, CFG_PENDING[+el.dataset.idx]);
  });
}

function destroyCfgGraphs() {
  for (const cy of CFG_INSTANCES) cy.destroy();
  CFG_INSTANCES.clear();
}

function mountCfg(el, dot) {
  const g = parseDot(dot);
  if (!g.nodes.length) {
    el.innerHTML = '<p class="cfg-empty">(empty graph)</p>';
    return;
  }
  if (typeof cytoscape !== "function") {
    el.innerHTML = '<p class="cfg-empty">(graph library failed to load)</p>';
    return;
  }
  // Measure the real monospace advance width (the label font is monospace,
  // so one measurement covers every character) so box sizing matches the
  // actual renderer.
  const meas = document.createElement("canvas").getContext("2d");
  meas.font = `${CFG_FONT.size}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
  const charW = Math.max(meas.measureText("M").width, 1);
  const elements = g.nodes.map(nd => {
    const box = labelBox(nd.label, charW);
    return {
      data: {
        id: "n" + nd.id,
        label: box.wrapped,
        code: nd.code,
        name: nd.label.split("\n")[0],
        w: box.w,
        h: box.h,
      },
    };
  });
  for (const [u, v] of g.edges) {
    elements.push({
      data: { id: `e${u}-${v}`, source: "n" + u, target: "n" + v },
      classes: u === v ? "loop" : "",
    });
  }
  const canvas = el.querySelector(".cfg-cy-canvas");
  const detail = el.querySelector(".cfg-cy-detail");
  const cy = cytoscape({
    container: canvas,
    elements,
    minZoom: 0.1,
    maxZoom: 4,
    style: [
      { selector: "node", style: {
        "background-color": CFG_COLORS.node,
        "border-color": CFG_COLORS.border,
        "border-width": 1.5,
        "shape": "round-rectangle",
        "width": "data(w)",
        "height": "data(h)",
        "label": "data(label)",
        "color": CFG_COLORS.text,
        "font-family": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        "font-size": CFG_FONT.size,
        // "wrap" is required: with "none" cytoscape collapses "\n" label
        // lines onto a single row. Our lines are pre-wrapped to fit, so no
        // extra word wraps occur.
        "text-wrap": "wrap",
        "text-max-width": `${CFG_FONT.maxW}px`,
        "text-valign": "center",
        "text-halign": "center",
        "padding": "0px",
        // Lift labels off the graticule dots behind the canvas.
        "text-outline-color": "#0c101b",
        "text-outline-width": 2,
        "text-outline-opacity": 0.9,
      }},
      { selector: "node#n0", style: { "border-color": CFG_COLORS.entry, "border-width": 2 } },
      { selector: "node:selected", style: { "border-color": CFG_COLORS.accent, "border-width": 2 } },
      { selector: "edge", style: {
        "width": 1.3,
        "line-color": CFG_COLORS.edge,
        "target-arrow-color": CFG_COLORS.edge,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.9,
        "curve-style": "bezier",
      }},
      { selector: "edge.back", style: {
        "line-color": CFG_COLORS.back,
        "target-arrow-color": CFG_COLORS.back,
      }},
      // Self-loop edges need explicit loop geometry or cytoscape refuses to
      // draw them ("invalid endpoints").
      { selector: "edge.loop", style: {
        "loop-direction": "-45deg",
        "loop-sweep": "-90deg",
        "control-point-step-size": 60,
      }},
    ],
  });
  // Lay out without the self-loop edges: dagre stamps them with unusable
  // control points ("invalid endpoints" warnings), and they add nothing to
  // the ranking. Loops render from their own style instead.
  cy.layout({
    name: "dagre", rankDir: "TB", nodeSep: 24, rankSep: 40, edgeSep: 12,
    eles: cy.elements().not(".loop"),
  }).run();
  // dagre lays cycles with their back edges running upward; mark them red.
  cy.edges().forEach(e => {
    if (e.target().position("y") <= e.source().position("y") + 1) e.addClass("back");
  });
  cy.fit(undefined, 24);
  // Tap a node to read its full block code (labels are truncated).
  cy.on("tap", "node", evt => {
    const nd = evt.target;
    const head = `<div class="cfg-cy-detail-head">${escapeHtml(nd.data("name"))}</div>`;
    detail.innerHTML = nd.data("code")
      ? `${head}<pre>${escapeHtml(nd.data("code"))}</pre>`
      : `${head}<p>(no instructions)</p>`;
  });
  cy.on("tap", evt => {
    if (evt.target === cy) detail.innerHTML = "";
  });
  // The lens readout: report the current zoom like a focus ring setting.
  const zoomEl = el.querySelector(".cfg-cy-zoom");
  if (zoomEl) {
    const showZoom = () => { zoomEl.textContent = "ZOOM ×" + cy.zoom().toFixed(2); };
    cy.on("zoom", showZoom);
    showZoom();
  }
  CFG_INSTANCES.add(cy);
  el._cy = cy;  // diagnostic hook
}

/* --- boot ---------------------------------------------------------------- */

function renderMeta(manifest) {
  const m = manifest.metadata;
  const tools = Object.values(m.toolVersions || {}).map(v =>
    v.replace(/\s*\(.*\)$/, "")).join(" · ");
  const errors = [];
  if (m.optCrashed) errors.push("opt failed (partial report)");
  if (m.llcCrashed) errors.push("llc failed (partial report)");
  document.getElementById("meta").textContent =
    `${m.source}  ·  ${m.pipeline}  ·  ${m.mtriple || "default triple"}  ·  ${tools}` +
    (errors.length ? "  ·  ⚠ " + errors.join(", ") : "");
}

async function route() {
  const manifest = await manifestPromise;
  destroyCfgGraphs();  // detach graph canvases from any previous view
  renderMeta(manifest);
  const match = location.hash.match(/^#\/pass\/(\d+)$/);
  if (match) await renderPassDetail(manifest, match[1]);
  else renderPipeline(manifest);
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
document.getElementById("changedOnly").addEventListener("change", route);
document.getElementById("fnFilter").addEventListener("input", route);
