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

function paneHtml(entries) {
  return entries.map(e =>
    e.keep ? escapeHtml(e.text)
           : `<span class="${e.text.length ? "del" : "add"}">${escapeHtml(e.text)}</span>`
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

async function renderPassDetail(manifest, id) {
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
            <pre>${paneHtml(d.before)}</pre>
          </div>
          <div class="diff-pane">
            <div class="head">after</div>
            <pre>${paneHtml(d.after)}</pre>
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
  return fns.map(([fn, change]) => {
    const parts = [];
    if (change.dotBefore) parts.push(`<details class="fn"><summary>${escapeHtml(fn)} · before</summary><pre class="raw">${escapeHtml(change.dotBefore)}</pre></details>`);
    if (change.dotAfter) parts.push(`<details class="fn" open><summary>${escapeHtml(fn)} · after</summary><pre class="raw">${escapeHtml(change.dotAfter)}</pre></details>`);
    return parts.join("");
  }).join("");
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
