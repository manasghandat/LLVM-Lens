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
  return `
    <div class="card ${p.changed ? "" : "unchanged"}" data-id="${p.id}">
      <div class="row">
        <span class="idx">#${p.runIndex}</span>
        <span class="name">${escapeHtml(p.name)}</span>
        ${badges.join(" ")}
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
  document.getElementById("app").innerHTML = `
    <div class="lanes">
      <div>
        <div class="lane-title"><h2>opt · IR passes</h2>
          <span class="count">${passes.filter(p => p.lane === "ir").length}</span></div>
        ${lane("ir") || '<div class="card unchanged"><div class="row">(no IR passes captured)</div></div>'}
      </div>
      <div>
        <div class="lane-title"><h2>llc · machine passes</h2>
          <span class="count">${passes.filter(p => p.lane === "mir").length}</span></div>
        ${lane("mir") || '<div class="card unchanged"><div class="row">(no machine passes captured)</div></div>'}
      </div>
      <div class="arrow">final IR ──&gt; ISel</div>
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
  if (tab === "Diff") body.innerHTML = diffTabHtml(data);
  else if (tab === "Analyses") body.innerHTML = analysesTabHtml(data);
  else if (tab === "Log") body.innerHTML = `<pre class="raw">${escapeHtml(data.log || "(no attributed output)")}</pre>`;
  else if (tab === "RegMap") body.innerHTML = regMapTabHtml(data);
  else if (tab === "CFG") body.innerHTML = cfgTabHtml(data);
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
    return `
      <details class="fn" open>
        <summary>${escapeHtml(fn)}</summary>
        <div class="diff-pair">
          <div class="diff-pane">
            <div class="head">before</div>
            <pre>${paneHtml(d.before, "del")}</pre>
          </div>
          <div class="diff-pane">
            <div class="head">after</div>
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
  const svg = cfgSvgHtml(dot);
  const body = svg
    ? `<div class="cfg-scroll">${svg}</div>
       <details><summary>raw DOT</summary><pre class="raw">${escapeHtml(dot)}</pre></details>`
    : `<p>(empty graph)</p>`;
  return `<details class="fn" ${open || side === "after" ? "open" : ""}><summary>${escapeHtml(fn)} · ${side}</summary>${body}</details>`;
}

/* --- CFG graph rendering (layered layout -> SVG, no deps) ----------------- */

const CFG = { cw: 7.4, lh: 13, px: 12, py: 7, gapX: 64, gapY: 26, wrap: 22 };

function parseDot(dot) {
  const nodes = [], edges = [];
  for (const line of String(dot || "").split("\n")) {
    let m = line.match(/^\s*n(\d+) \[label="((?:[^"\\]|\\.)*)"\]\s*;?$/);
    if (m) {
      const raw = m[2];
      nodes.push({ id: +m[1], label: raw.replace(/\\n/g, "\n").replace(/\\(.)/g, "$1") });
      continue;
    }
    m = line.match(/^\s*n(\d+) -> n(\d+);$/);
    if (m) edges.push([+m[1], +m[2]]);
  }
  return { nodes, edges };
}

function wrapLabel(text, width) {
  const out = [];
  for (const part of String(text).split("\n")) {
    let line = "";
    for (const word of part.split(" ")) {
      if (!line) line = word;
      else if (line.length + 1 + word.length <= width) line += " " + word;
      else { out.push(line); line = word; }
    }
    if (line) out.push(line);
  }
  return out.length ? out : [""];
}

// Longest-path layering (entry = node 0 at layer 0); cycles terminate via
// the visited set. Back edges (target not strictly below source) are flagged.
function layoutCfg({ nodes, edges }) {
  const n = nodes.length;
  const layer = new Array(n).fill(0);
  const done = new Array(n).fill(false);
  const preds = Array.from({ length: n }, () => []);
  for (const [u, v] of edges) preds[v].push(u);
  const visit = (v) => {
    if (done[v]) return;
    done[v] = true;
    for (const u of preds[v]) { visit(u); layer[v] = Math.max(layer[v], layer[u] + 1); }
  };
  for (let i = 0; i < n; i++) visit(i);

  const back = new Set();
  for (const [u, v] of edges) if (layer[v] <= layer[u]) back.add(u + ">" + v);

  const byLayerMap = new Map();
  for (let i = 0; i < n; i++) {
    const L = layer[i];
    if (!byLayerMap.has(L)) byLayerMap.set(L, []);
    byLayerMap.get(L).push(i);
  }
  const byLayer = [...byLayerMap.entries()].sort((a, b) => a[0] - b[0]).map(e => e[1]);

  const box = nodes.map(nd => {
    const lines = wrapLabel(nd.label, CFG.wrap);
    return { w: Math.max(...lines.map(l => l.length)) * CFG.cw + 2 * CFG.px,
             h: lines.length * CFG.lh + 2 * CFG.py, lines };
  });

  const layerH = byLayer.map(l => Math.max(...l.map(i => box[i].h)));
  const layerY = [];
  let y = 0;
  for (let L = 0; L < byLayer.length; L++) { layerY[L] = y; y += layerH[L] + CFG.gapY; }

  const pos = new Array(n);
  for (let L = 0; L < byLayer.length; L++) {
    const ids = byLayer[L];
    const totalW = ids.reduce((s, i) => s + box[i].w, 0) + CFG.gapX * (ids.length - 1);
    let x = -totalW / 2;
    for (const i of ids) { pos[i] = { x, y: layerY[L] + (layerH[L] - box[i].h) / 2 }; x += box[i].w + CFG.gapX; }
  }
  let minX = 0, maxX = 0;
  for (let i = 0; i < n; i++) {
    minX = Math.min(minX, pos[i].x);
    maxX = Math.max(maxX, pos[i].x + box[i].w);
  }
  for (let i = 0; i < n; i++) pos[i].x -= minX;
  return { pos, box, back, W: maxX - minX, H: layerY[byLayer.length - 1] + layerH[byLayer.length - 1] };
}

function cfgSvgHtml(dot) {
  const g = parseDot(dot);
  if (!g.nodes.length) return "";
  const laid = layoutCfg(g);
  const uid = "cfg" + Math.random().toString(36).slice(2, 8);
  const parts = [
    `<svg class="cfg" width="${laid.W}" height="${laid.H}" viewBox="0 0 ${laid.W} ${laid.H}" xmlns="http://www.w3.org/2000/svg">`,
    `<defs><marker id="${uid}-a" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 z"/></marker></defs>`,
  ];
  for (const [u, v] of g.edges) {
    const a = laid.pos[u], b = laid.pos[v], ab = laid.box[u], bb = laid.box[v];
    const isBack = laid.back.has(u + ">" + v);
    let d;
    if (u === v) {
      const r = 16;
      d = `M ${a.x + ab.w} ${a.y + ab.h / 2} C ${a.x + ab.w + r} ${a.y + ab.h / 2 - r}, ${a.x + ab.w + r} ${a.y + ab.h / 2 + r}, ${a.x + ab.w} ${a.y + ab.h / 2 + r}`;
    } else if (isBack) {
      d = `M ${a.x + ab.w} ${a.y + ab.h / 2} C ${a.x + ab.w + 42} ${a.y + ab.h / 2}, ${b.x + bb.w + 42} ${b.y + bb.h / 2}, ${b.x + bb.w} ${b.y + bb.h / 2}`;
    } else {
      const y0 = a.y + ab.h, y1 = b.y, mid = Math.max(24, (y1 - y0) / 2);
      d = `M ${a.x + ab.w / 2} ${y0} C ${a.x + ab.w / 2} ${y0 + mid}, ${b.x + bb.w / 2} ${y1 - mid}, ${b.x + bb.w / 2} ${y1}`;
    }
    parts.push(`<path class="${isBack ? "back" : ""}" d="${d}" marker-end="url(#${uid}-a)"/>`);
  }
  for (const nd of g.nodes) {
    const p = laid.pos[nd.id], b = laid.box[nd.id];
    parts.push(
      `<g class="node${nd.id === 0 ? " entry" : ""}" transform="translate(${p.x},${p.y})">` +
      `<rect width="${b.w}" height="${b.h}" rx="4"/>` +
      `<text x="${b.w / 2}" y="${CFG.py + CFG.lh / 2}">` +
      b.lines.map((ln, k) =>
        `<tspan class="${k === 0 ? "bn" : "c"}" x="${b.w / 2}" dy="${k ? CFG.lh : 0}">${escapeHtml(ln)}</tspan>`
      ).join("") +
      `</text></g>`
    );
  }
  parts.push("</svg>");
  return parts.join("");
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
  renderMeta(manifest);
  const match = location.hash.match(/^#\/pass\/(\d+)$/);
  if (match) await renderPassDetail(manifest, match[1]);
  else renderPipeline(manifest);
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", route);
document.getElementById("changedOnly").addEventListener("change", route);
document.getElementById("fnFilter").addEventListener("input", route);
