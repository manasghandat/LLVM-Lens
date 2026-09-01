/* LLVM-Lens report viewer. Single-screen workstation, file://-safe.
 * Left rail: pass list (lane tabs: IR / machine) + function list, both
 * collapsible. Right: split main view with CFG and Diff panes (side by side
 * or stacked, draggable divider), and a collapsible bottom panel with
 * Log / Analyses / RegMap / Asm tabs.
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

/* --- llvm ir highlighting -------------------------------------------------- */

// Token vocabulary modeled on the llvm-syntax-highlighting TextMate grammar
// (keywords / types / literals / block labels / strings / numbers / comments
// / decorators / function headers), plus MIR additions (line-leading
// opcodes, $registers). Line-based scanner: first match at a position wins;
// everything else passes through escaped.
const IR_TOKEN_DEFS = [
  { cls: "tok-com", re: /;[^\n]*/ },
  { cls: "tok-str", re: /c?"(?:\\.|[^"\\])*"/ },
  { cls: "tok-md", re: /[#!][A-Za-z0-9.]+/ },                    // !dbg !36, #0
  { cls: "tok-fn", re: /@[A-Za-z_.$][\w.$-]*(?=\()/ },           // @fn( header
  { cls: "tok-label", re: /^[ \t]*[A-Za-z0-9._$-]+(?: \([^)]*\))?:\s*$/ },  // block: (definition line)
  { cls: "tok-op", re: /(?<=^[ \t]*)[A-Z][A-Z0-9_]*(?=[ \t]|$)/ },  // MIR opcodes
  { cls: "tok-type", re: /(?:%struct\.[\w.]*|<(?:i\d+|ptr)>|<\d+ x [^>]*>|\[\d+ x [^\]]*\]|\b(?:i\d+|ptr|void|half|bfloat|float|double|fp128|x86_fp80|ppc_fp128)\b)/ },
  { cls: "tok-lit", re: /\b(?:true|false|null|none|undef|poison|zeroinitializer)\b/ },
  { cls: "tok-num", re: /(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])/ },
  { cls: "tok-kw", re: /\b(?:add|alloca|and|ashr|atomicrmw|attributes|bitcast|br|call|catchpad|catchret|catchswitch|cleanuppad|cleanupret|cmpxchg|declare|define|exact|extractelement|extractvalue|fadd|fcmp|fdiv|fence|fmul|fpext|fptosi|fptoui|fptrunc|freeze|frem|fsub|getelementptr|global|icmp|indirectbr|insertelement|insertvalue|inttoptr|invoke|landingpad|load|lshr|mul|musttail|notail|one|or|ord|phi|ptrtoint|resume|ret|sdiv|select|sext|shl|shufflevector|sitofp|srem|store|sub|switch|tail|target|to|trunc|type|udiv|uitofp|unreachable|unwind|urem|va_arg|xor|zext|eq|ne|ugt|uge|ult|ule|sgt|sge|slt|sle|nuw|nsw|inbounds|align|volatile|atomic|syncscope|acquire|release|acq_rel|seq_cst|monotonic|private|internal|available_externally|linkonce|weak|common|appending|extern_weak|linkonce_odr|weak_odr|external|thread_local|localdynamic|initialexec|localexec|hidden|protected|default|dllimport|dllexport|section|unnamed_addr|local_unnamed_addr|externally_initialized|personality|comdat|source_filename|datalayout|triple|module|asm|noreturn|nounwind|readnone|readonly|noinline|alwaysinline|inlinehint|optsize|optnone|uwtable|cold|hot|minsize|naked|noimplicitfloat|norecurse|noredzone|nonlazybind|nobuiltin|noduplicate|convergent|jumptable|returns_twice|returned|willreturn|writeonly|safestack|ssp|sspreq|sspstrong|strictfp|signext|zeroext|inreg|byval|sret|nested|nonnull|dereferenceable|swiftself|swifterror|immarg|noalias|nocapture)\b/ },
  { cls: "tok-var", re: /[%@$][-A-Za-z$._0-9]*/ },               // %x %42 @g $eflags
];
const IR_TOKEN_RES = IR_TOKEN_DEFS.map(d =>
  ({ cls: d.cls, re: new RegExp(d.re.source, "g") }));

function highlightIR(text) {
  return String(text).split("\n").map(line => {
    if (!line.trim()) return "";
    const spans = [];
    for (const { cls, re } of IR_TOKEN_RES) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(line))) {
        if (!m[0]) { re.lastIndex++; continue; }
        spans.push({ s: m.index, e: m.index + m[0].length, cls });
      }
    }
    // Stable sort by start; first definition wins on ties, overlaps skip.
    spans.sort((a, b) => a.s - b.s);
    let out = "", pos = 0;
    for (const sp of spans) {
      if (sp.s < pos) continue;
      out += escapeHtml(line.slice(pos, sp.s));
      out += `<span class="${sp.cls}">${escapeHtml(line.slice(sp.s, sp.e))}</span>`;
      pos = sp.e;
    }
    return out + escapeHtml(line.slice(pos));
  }).join("\n");
}

function paneHtml(entries, kind) {
  // kind is "del" for the before pane (removed/changed lines, red) and
  // "add" for the after pane (added/changed lines, green). Lines are
  // tokenized for LLVM IR; the wrapper carries the diff signal.
  return entries.map(e => {
    const body = highlightIR(e.text);
    return e.keep ? body : `<span class="${kind}">${body}</span>`;
  }).join("\n");
}

/* --- views ---------------------------------------------------------------- */

// Workspace state. One screen, no routes: lane tabs and the function list
// drive which pass/function the split view and bottom panel show.
let STATE = {
  lane: "ir",                // "ir" | "mir" — active lane tab
  passId: null,              // selected pass id (manifest)
  fn: null,                  // selected function name
  mode: "both",              // main view: "cfg" | "diff" | "both"
  orientation: "side",       // "side" (side by side) | "stack" (stacked)
  cfgSource: "after",        // CFG pane source: before | after | both
  bottomOpen: true,
  bottomTab: null,
  splitRatio: 0.5,           // first pane share of the split area
};
let CURRENT_MANIFEST = null;   // manifest.json
let CURRENT_PASS = null;       // loaded chunk for STATE.passId

function passSummaries() { return (CURRENT_MANIFEST || { passes: [] }).passes; }
function currentPassSummary() { return passSummaries().find(p => p.id === STATE.passId); }
function fnNames() {
  const s = currentPassSummary();
  return (s && s.functions) || [];
}
function fnChange(fn) {
  return CURRENT_PASS && CURRENT_PASS.functions && CURRENT_PASS.functions[fn];
}

/* --- rail: pass list ------------------------------------------------------ */

function renderLaneTabs() {
  document.querySelectorAll("#passPanel .ptab").forEach(b =>
    b.classList.toggle("active", b.dataset.lane === STATE.lane));
}

function renderPassList() {
  const onlyChanged = document.getElementById("changedOnly").checked;
  const passes = passSummaries().filter(p =>
    p.lane === STATE.lane &&
    (!onlyChanged || p.changed || p.lane === "mir" || p.isCustom));
  const rows = passes.map(p => {
    const a = p.analysisCounts || {};
    const idx = String(p.runIndex).padStart(3, "0");
    return `
      <div class="row ${p.changed || p.isCustom ? "" : "dim"} ${p.id === STATE.passId ? "sel" : ""}" data-id="${p.id}">
        <span class="idx">#${idx}</span>
        <span class="name">${escapeHtml(p.name)}</span>
        ${p.isCustom ? '<span class="badge" title="custom pass (--custom-pass)">custom</span>' : ""}
        ${p.changed ? '<span class="dot" title="changed IR"></span>' : ""}
        ${p.spillCount ? `<span class="warn" title="${p.spillCount} spills">⚠${p.spillCount}</span>` : ""}
        <span class="stat" title="analyses run / invalidated">+${a.run || 0} −${a.invalidated || 0}</span>
        <span class="time">${p.timeMs != null ? p.timeMs.toFixed(2) + " ms" : ""}</span>
      </div>`;
  }).join("");
  document.getElementById("passList").innerHTML =
    rows || `<div class="row empty">(no ${STATE.lane === "ir" ? "IR" : "machine"} passes captured)</div>`;
  document.querySelectorAll("#passList .row[data-id]").forEach(r =>
    r.addEventListener("click", () => selectPass(+r.dataset.id)));
}

async function selectPass(id) {
  STATE.passId = id;
  STATE.fn = null;
  CURRENT_PASS = null;
  renderPassList();
  renderFnList();
  renderMain();
  renderBottom();
  const data = await loadPass(id);
  if (STATE.passId !== id) return;  // user switched passes while loading
  CURRENT_PASS = data;
  // Default to the first changed function, else the first function.
  const names = fnNames();
  STATE.fn = names.find(f => fnChange(f) && fnChange(f).changed) || names[0] || null;
  renderFnList();
  renderMain();
  renderBottom();
  renderCtx();
}

/* --- rail: function list --------------------------------------------------- */

function renderFnList() {
  const q = (document.getElementById("fnFilter") || {}).value || "";
  const names = fnNames().filter(n => !q || n.toLowerCase().includes(q.toLowerCase()));
  const rows = names.map(n => {
    const ch = fnChange(n);
    const changed = ch && ch.changed;
    let stat = "";
    if (changed) {
      const d = diffLines(ch.before, ch.after);
      const del = d.before.filter(e => !e.keep).length;
      const add = d.after.filter(e => !e.keep).length;
      stat = `<span class="stat"><span class="minus">−${del}</span> <span class="plus">+${add}</span></span>`;
    }
    return `
      <div class="row ${changed ? "" : "dim"} ${n === STATE.fn ? "sel" : ""}" data-fn="${escapeHtml(n)}">
        <span class="name">${escapeHtml(n)}</span>
        ${changed ? '<span class="dot"></span>' : ""}
        ${stat}
      </div>`;
  }).join("");
  document.getElementById("fnList").innerHTML =
    rows || (fnNames().length
      ? '<div class="row empty">(no functions match the filter)</div>'
      : '<div class="row empty">(no functions captured)</div>');
  document.getElementById("fnCount").textContent = fnNames().length;
  document.querySelectorAll("#fnList .row[data-fn]").forEach(r =>
    r.addEventListener("click", () => {
      STATE.fn = r.dataset.fn;
      renderFnList();
      renderMain();
      renderCtx();
    }));
}

/* --- main view: context line, panes ---------------------------------------- */

function renderCtx() {
  const s = currentPassSummary();
  document.getElementById("ctx").innerHTML = s
    ? `${escapeHtml(s.name)} · <span class="fn">#${String(s.runIndex).padStart(3, "0")}</span>`
      + (s.isCustom ? ' · <span class="badge">custom</span>' : "")
      + (STATE.fn ? ` · fn <span class="fn">${escapeHtml(STATE.fn)}</span>` : "")
    : "no pass selected";
}

function updateViewHead() {
  const set = (el, on) => { el.classList.toggle("on", on); el.classList.toggle("off", !on); };
  document.querySelectorAll("#modeCtl .chip").forEach(b =>
    set(b, b.dataset.mode === STATE.mode));
  // Orientation only applies when both panes are shown.
  document.getElementById("splitCtl").classList.toggle("inactive", STATE.mode !== "both");
  set(document.getElementById("splitSide"), STATE.orientation === "side");
  set(document.getElementById("splitStack"), STATE.orientation === "stack");
}

function pane(title, chips, stat, body) {
  return `
    <section class="pane">
      <div class="pane-head">
        <span class="pane-title">${title}</span>${chips}
        <span class="pane-stat">${stat}</span>
      </div>
      <div class="pane-body">${body}</div>
    </section>`;
}

function cfgPaneHtml() {
  const ch = fnChange(STATE.fn);
  const dotBefore = ch && ch.dotBefore;
  const dotAfter = ch && ch.dotAfter;
  const srcs = [];
  if (dotBefore) srcs.push("before");
  if (dotAfter) srcs.push("after");
  if (dotBefore && dotAfter) srcs.push("both");
  if (!srcs.length) {
    return pane("CFG", "", "", '<div class="cfg-empty">(no CFG data'
      + (STATE.fn ? ` for ${escapeHtml(STATE.fn)}` : "") + ")</div>");
  }
  if (!srcs.includes(STATE.cfgSource)) STATE.cfgSource = srcs[srcs.length - 1];
  const chips = srcs.map(s =>
    `<button class="ptab ${s === STATE.cfgSource ? "active" : ""}" data-src="${s}">${s}</button>`
  ).join("");
  const body = STATE.cfgSource === "both"
    ? `<div class="cfg-pair">${cfgBodyHtml(dotBefore, "before")}${cfgBodyHtml(dotAfter, "after")}</div>`
    : cfgBodyHtml(STATE.cfgSource === "before" ? dotBefore : dotAfter, STATE.cfgSource);
  return pane("CFG", chips, "", body);
}

function diffPaneHtml() {
  const ch = fnChange(STATE.fn);
  if (!ch) return pane("DIFF", "", "", '<div class="cfg-empty">(select a function)</div>');
  if (!ch.changed) {
    return pane("DIFF", "", "",
      `<div class="cfg-empty">(${escapeHtml(STATE.fn)} unchanged — nothing to diff)</div>`);
  }
  const d = diffLines(ch.before, ch.after);
  const del = d.before.filter(e => !e.keep).length;
  const add = d.after.filter(e => !e.keep).length;
  const stat = `<span class="minus">−${del}</span> <span class="plus">+${add}</span> · ${escapeHtml(STATE.fn)}`;
  const body = `
    <div class="dpair">
      <div class="diff-pane">
        <div class="head"><span>before</span><span class="count del">−${del}</span></div>
        <pre>${paneHtml(d.before, "del")}</pre>
      </div>
      <div class="diff-pane">
        <div class="head"><span>after</span><span class="count add">+${add}</span></div>
        <pre>${paneHtml(d.after, "add")}</pre>
      </div>
    </div>`;
  return pane("DIFF", "", stat, body);
}

function renderMain() {
  updateViewHead();
  renderCtx();
  destroyCfgGraphs();
  const split = document.getElementById("split");
  split.className = "split" + (STATE.orientation === "stack" ? " stacked" : "");
  const panes = [];
  if (STATE.mode !== "diff") panes.push(cfgPaneHtml());
  if (STATE.mode !== "cfg") panes.push(diffPaneHtml());
  split.innerHTML = panes.length === 2
    ? panes[0] + '<div class="divider" title="drag to resize"></div>' + panes[1]
    : panes.join("");
  const first = split.querySelector(".pane");
  if (panes.length === 2 && first) {
    first.style.flex = `0 0 ${(STATE.splitRatio * 100).toFixed(1)}%`;
  }
  mountCfgGraphs();
}

/* --- bottom panel ---------------------------------------------------------- */

function bottomTabs() {
  const tabs = ["Log", "Analyses"];
  if (CURRENT_PASS && CURRENT_PASS.lane === "mir") tabs.push("RegMap");
  if (CURRENT_PASS && CURRENT_PASS.lane === "mir" && CURRENT_PASS.asm) tabs.push("Asm");
  return tabs;
}

function renderBottom() {
  const tabs = bottomTabs();
  if (!tabs.includes(STATE.bottomTab)) STATE.bottomTab = "Log";
  document.getElementById("bottomTabs").innerHTML = tabs.map(t =>
    `<button class="vtab ${t === STATE.bottomTab ? "active" : ""}" data-tab="${t}">${t}</button>`
  ).join("");
  document.getElementById("bottom").classList.toggle("open", STATE.bottomOpen);
  const chev = document.getElementById("bottomChev");
  chev.textContent = STATE.bottomOpen ? "⌄" : "▸ collapsed";
  chev.classList.toggle("collapsed", !STATE.bottomOpen);
  chev.title = STATE.bottomOpen ? "collapse panel" : "expand panel";
  document.getElementById("bottomBody").innerHTML = STATE.bottomOpen ? bottomBodyHtml() : "";
  document.querySelectorAll("#bottomTabs .vtab").forEach(b =>
    b.addEventListener("click", () => { STATE.bottomTab = b.dataset.tab; renderBottom(); }));
}

function bottomBodyHtml() {
  const d = CURRENT_PASS;
  if (!d) return '<p class="cfg-empty">(select a pass)</p>';
  if (STATE.bottomTab === "Log") {
    return `<pre class="raw">${escapeHtml(d.log || "(no attributed output)")}</pre>`;
  }
  if (STATE.bottomTab === "Analyses") {
    const a = d.analyses || {};
    return [["run", a.run], ["cached", a.cached], ["invalidated", a.invalidated]]
      .map(([label, list]) => `
        <h3>${label} (${(list || []).length})</h3>
        <pre class="raw">${escapeHtml((list || []).join("\n") || "(none)")}</pre>`)
      .join("");
  }
  if (STATE.bottomTab === "RegMap") {
    const map = (d.regMap || {})[STATE.fn];
    if (!map) {
      return `<p class="cfg-empty">(no vreg assignments for ${STATE.fn ? escapeHtml(STATE.fn) : "the selected function"})</p>`;
    }
    return `<table class="grid"><tr><th>vreg</th><th>physreg / slot</th></tr>`
      + Object.entries(map)
        .map(([v, p]) => `<tr><td>%${escapeHtml(v)}</td><td>${escapeHtml(p)}</td></tr>`)
        .join("") + "</table>";
  }
  if (STATE.bottomTab === "Asm") return `<pre class="raw">${escapeHtml(d.asm)}</pre>`;
  return "";
}

/* --- CFG graph rendering (cytoscape + dagre, vendored in vendor/) --------- */

// Per-render state: DOT payloads indexed as cfgBodyHtml builds the HTML, and
// live cytoscape instances that get destroyed on re-render.
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

function cfgBodyHtml(dot, label) {
  const idx = CFG_PENDING.length;
  CFG_PENDING.push(dot);
  return `
    <div class="cfg-cy" data-idx="${idx}">
      <div class="cfg-cy-frame">
        <span class="flabel">${escapeHtml(label || "")}</span>
        <i class="cb tl" aria-hidden="true"></i><i class="cb tr" aria-hidden="true"></i>
        <i class="cb bl" aria-hidden="true"></i><i class="cb br" aria-hidden="true"></i>
        <div class="cfg-cy-canvas"></div>
        <div class="cfg-cy-zoom">ZOOM ×1.00</div>
      </div>
      <div class="cfg-cy-detail"></div>
    </div>`;
}

function mountCfgGraphs() {
  document.querySelectorAll("#split .cfg-cy").forEach(el => {
    mountCfg(el, CFG_PENDING[+el.dataset.idx]);
  });
}

function destroyCfgGraphs() {
  for (const cy of CFG_INSTANCES) cy.destroy();
  CFG_INSTANCES.clear();
  CFG_PENDING = [];
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
      ? `${head}<pre>${highlightIR(nd.data("code"))}</pre>`
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
  const plugins = (m.plugins && m.plugins.length)
    ? `  ·  ${m.plugins.length} plugin${m.plugins.length > 1 ? "s" : ""}` : "";
  document.getElementById("meta").textContent =
    `${m.source}  ·  ${m.pipeline}  ·  ${m.mtriple || "default triple"}  ·  ${tools}${plugins}` +
    (errors.length ? "  ·  ⚠ " + errors.join(", ") : "");
}

function resizeGraphs() {
  for (const cy of CFG_INSTANCES) cy.resize();
}

async function boot() {
  const manifest = await manifestPromise;
  CURRENT_MANIFEST = manifest;
  renderMeta(manifest);
  renderLaneTabs();
  renderPassList();
  renderFnList();
  renderMain();
  renderBottom();
  const first = manifest.passes.find(p => p.lane === STATE.lane);
  if (first) selectPass(first.id);
}

/* --- controls --------------------------------------------------------------- */

document.querySelectorAll("#passPanel .ptab").forEach(b =>
  b.addEventListener("click", () => {
    if (STATE.lane === b.dataset.lane) return;
    STATE.lane = b.dataset.lane;
    STATE.passId = null;
    STATE.fn = null;
    CURRENT_PASS = null;
    renderLaneTabs();
    renderPassList();
    renderFnList();
    renderMain();
    renderBottom();
    const first = passSummaries().find(p => p.lane === STATE.lane);
    if (first) selectPass(first.id);
  }));

document.getElementById("modeCtl").addEventListener("click", evt => {
  const b = evt.target.closest(".chip[data-mode]");
  if (!b) return;
  STATE.mode = b.dataset.mode;
  renderMain();
});
document.getElementById("splitSide").addEventListener("click", () => {
  STATE.orientation = "side";
  renderMain();
});
document.getElementById("splitStack").addEventListener("click", () => {
  STATE.orientation = "stack";
  renderMain();
});

// CFG source chips (before / after / final) live inside the rebuilt pane.
document.getElementById("split").addEventListener("click", evt => {
  const b = evt.target.closest(".ptab[data-src]");
  if (!b) return;
  STATE.cfgSource = b.dataset.src;
  renderMain();
});

// Bottom panel collapse + tab switching.
document.getElementById("bottomChev").addEventListener("click", () => {
  STATE.bottomOpen = !STATE.bottomOpen;
  renderBottom();
});

// Rail collapse / expand.
document.getElementById("railCollapse").addEventListener("click", () => {
  document.body.classList.add("rail-collapsed");
  resizeGraphs();
});
document.getElementById("railExpand").addEventListener("click", () => {
  document.body.classList.remove("rail-collapsed");
  resizeGraphs();
});

// Split divider drag: the first pane's share follows the pointer.
{
  const splitEl = document.getElementById("split");
  splitEl.addEventListener("pointerdown", evt => {
    if (!evt.target.closest(".divider")) return;
    if (splitEl.querySelectorAll(".pane").length < 2) return;
    const stacked = splitEl.classList.contains("stacked");
    const first = splitEl.querySelector(".pane");
    const rect = splitEl.getBoundingClientRect();
    const total = stacked ? rect.height : rect.width;
    const startPos = stacked ? evt.clientY : evt.clientX;
    const startSize = stacked
      ? first.getBoundingClientRect().height
      : first.getBoundingClientRect().width;
    const move = e => {
      const delta = (stacked ? e.clientY : e.clientX) - startPos;
      const ratio = Math.min(0.85, Math.max(0.15, (startSize + delta) / total));
      first.style.flex = `0 0 ${(ratio * 100).toFixed(1)}%`;
    };
    const up = () => {
      const m = first.style.flex.match(/[\d.]+/);
      if (m) STATE.splitRatio = parseFloat(m[0]) / 100;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      resizeGraphs();  // graphs track the new pane size
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

document.getElementById("changedOnly").addEventListener("change", renderPassList);
document.getElementById("fnFilter").addEventListener("input", renderFnList);

window.addEventListener("DOMContentLoaded", boot);
