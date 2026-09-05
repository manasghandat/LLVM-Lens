/* LLVM-Lens report viewer. Single-screen workstation, file://-safe.
 * Left rail: pass list (lane tabs: IR / machine) + function list, both
 * collapsible. Right: the main view, one of three -- CFG (cytoscape graphs),
 * Diff (unified/git-style: hunks, line numbers, +/- markers) or IR (the whole
 * before/after snapshots, side by side or stacked with a draggable divider)
 * -- plus a collapsible bottom panel with Log / Analyses / RegMap / Asm tabs.
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

// Unified, git-style diffs: an op stream (context / removed / added lines
// with their before/after line numbers), collapsed into hunks with a few
// lines of context around each change. Rendering lives in unifiedDiffHtml.

const DIFF_CONTEXT = 3;   // unchanged lines kept around a change (git default)

function splitLines(text) {
  if (!text) return [];
  return String(text).replace(/\n$/, "").split("\n");
}

// Git prints every removal of a change block before that block's additions;
// the LCS walk below can interleave them, so regroup each run of changes.
function groupChanges(ops) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length) {
      out.push(...run.filter(o => o.op === "-"), ...run.filter(o => o.op === "+"));
      run = [];
    }
  };
  for (const op of ops) {
    if (op.op === " ") { flush(); out.push(op); } else run.push(op);
  }
  flush();
  return out;
}

// Line diff -> [{ op: " " | "-" | "+", text, a, b }], where a/b are 1-based
// line numbers in the before/after text (null on the side lacking the line).
function diffOps(beforeText, afterText) {
  const A = splitLines(beforeText), B = splitLines(afterText);

  // Trim the common head and tail before the O(n*m) table below: a pass
  // usually rewrites a few lines in the middle of an otherwise equal
  // function, so this keeps the table small on real IR.
  let head = 0;
  while (head < A.length && head < B.length && A[head] === B[head]) head++;
  let tail = 0;
  while (tail < A.length - head && tail < B.length - head
         && A[A.length - 1 - tail] === B[B.length - 1 - tail]) tail++;

  const a = A.slice(head, A.length - tail);
  const b = B.slice(head, B.length - tail);
  const n = a.length, m = b.length;

  // dp[i][j] = LCS length of a[i:] and b[j:].
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j]
        ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);

  const ops = [];
  for (let k = 0; k < head; k++)
    ops.push({ op: " ", text: A[k], a: k + 1, b: k + 1 });

  const middle = [];
  for (let i = 0, j = 0; i < n || j < m; ) {
    if (i < n && j < m && a[i] === b[j]) {
      middle.push({ op: " ", text: a[i], a: head + i + 1, b: head + j + 1 });
      i++; j++;
    } else if (j >= m || (i < n && dp[i + 1][j] >= dp[i][j + 1])) {
      middle.push({ op: "-", text: a[i], a: head + i + 1, b: null });
      i++;
    } else {
      middle.push({ op: "+", text: b[j], a: null, b: head + j + 1 });
      j++;
    }
  }
  ops.push(...groupChanges(middle));

  for (let k = 0; k < tail; k++)
    ops.push({
      op: " ", text: A[A.length - tail + k],
      a: A.length - tail + k + 1, b: B.length - tail + k + 1,
    });
  return ops;
}

function diffStat(beforeText, afterText) {
  const ops = diffOps(beforeText, afterText);
  return {
    ops,
    del: ops.filter(o => o.op === "-").length,
    add: ops.filter(o => o.op === "+").length,
  };
}

// Collapse unchanged stretches into hunks keeping `context` lines around each
// change. Infinite context yields a single hunk covering the whole function.
// Each hunk carries its @@ header plus how many lines were hidden before it.
function diffHunks(ops, context = DIFF_CONTEXT) {
  const keep = new Array(ops.length).fill(false);
  let changes = 0;
  ops.forEach((op, i) => {
    if (op.op === " ") return;
    changes++;
    for (let k = Math.max(0, i - context);
         k <= Math.min(ops.length - 1, i + context); k++) keep[k] = true;
  });
  if (!changes) return [];

  const hunks = [];
  let current = null, hidden = 0;
  for (let i = 0; i < ops.length; i++) {
    if (!keep[i]) { current = null; hidden++; continue; }
    if (!current) { current = { rows: [], hidden }; hunks.push(current); hidden = 0; }
    current.rows.push(ops[i]);
  }

  for (const hunk of hunks) {
    const olds = hunk.rows.filter(r => r.op !== "+");
    const news = hunk.rows.filter(r => r.op !== "-");
    hunk.oldStart = olds.length ? olds[0].a : 0;
    hunk.newStart = news.length ? news[0].b : 0;
    hunk.oldCount = olds.length;
    hunk.newCount = news.length;
    hunk.header =
      `@@ -${hunk.oldStart},${hunk.oldCount} +${hunk.newStart},${hunk.newCount} @@`;
  }
  return hunks;
}

// One unified diff: per hunk an @@ header row, then rows of
// [old line no.][new line no.][+/-/space marker][tokenized line].
function unifiedDiffHtml(hunks) {
  return hunks.map(hunk => {
    const hidden = hunk.hidden
      ? `<span class="uskip">${hunk.hidden} unchanged `
        + `line${hunk.hidden === 1 ? "" : "s"} hidden</span>`
      : "";
    const rows = hunk.rows.map(row => {
      // Namespaced classes: a bare "ctx" would collide with the view's own
      // .ctx rule, whose overflow:hidden would break the sticky gutter.
      const cls = row.op === "+" ? "add" : row.op === "-" ? "del" : "uctx";
      const body = highlightIR(row.text);
      return `<div class="urow ${cls}">`
        + `<span class="uln">${row.a || ""}</span>`
        + `<span class="uln">${row.b || ""}</span>`
        + `<span class="umark">${row.op === " " ? "&nbsp;" : row.op}</span>`
        + `<code class="utext">${body || "&nbsp;"}</code>`
        + "</div>";
    }).join("");
    return `<div class="uhunk"><div class="uhead">`
      + `<span class="usticky"><span class="uhh">${escapeHtml(hunk.header)}</span>`
      + `${hidden}</span></div>${rows}</div>`;
  }).join("");
}

// One side of the IR view: every line of that snapshot, numbered on its own
// side, with the lines this pass touched tinted. `mark` picks the side --
// "-" keeps context plus removals (the before text), "+" context plus
// additions. Nothing is collapsed: this view is for reading the whole
// function, not just the change.
function irSideHtml(ops, mark) {
  return ops.filter(o => o.op === " " || o.op === mark).map(o => {
    const cls = o.op === " " ? "uctx" : (mark === "+" ? "add" : "del");
    return `<div class="urow ${cls}">`
      + `<span class="uln">${mark === "+" ? o.b : o.a}</span>`
      + `<code class="utext">${highlightIR(o.text) || "&nbsp;"}</code>`
      + "</div>";
  }).join("");
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

/* --- c source highlighting ------------------------------------------------ */

// Deliberately shallow: enough structure to read a C file next to the IR,
// reusing the same .tok-* palette. Comments and strings win over keywords,
// so a keyword inside a string stays plain.
const C_TOKEN_RES = [
  { cls: "tok-com", re: /\/\/[^\n]*|\/\*[\s\S]*?\*\// },
  { cls: "tok-md", re: /^[ \t]*#\s*\w+/ },                       // preprocessor
  { cls: "tok-str", re: /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/ },
  { cls: "tok-type", re: /\b(?:void|char|short|int|long|float|double|signed|unsigned|_Bool|size_t|ssize_t|ptrdiff_t|u?int(?:8|16|32|64)_t|FILE|struct|union|enum)\b/ },
  { cls: "tok-kw", re: /\b(?:auto|break|case|const|continue|default|do|else|extern|for|goto|if|inline|register|restrict|return|sizeof|static|switch|typedef|volatile|while)\b/ },
  { cls: "tok-lit", re: /\b(?:NULL|true|false)\b/ },
  { cls: "tok-num", re: /\b(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)\b/ },
  { cls: "tok-fn", re: /\b[A-Za-z_]\w*(?=\s*\()/ },
].map(d => ({ cls: d.cls, re: new RegExp(d.re.source, "g") }));

function highlightC(line) {
  const spans = [];
  for (const { cls, re } of C_TOKEN_RES) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(line))) {
      if (!m[0]) { re.lastIndex++; continue; }
      spans.push({ s: m.index, e: m.index + m[0].length, cls });
    }
  }
  spans.sort((a, b) => a.s - b.s);
  let out = "", pos = 0;
  for (const sp of spans) {
    if (sp.s < pos) continue;
    out += escapeHtml(line.slice(pos, sp.s));
    out += `<span class="${sp.cls}">${escapeHtml(line.slice(sp.s, sp.e))}</span>`;
    pos = sp.e;
  }
  return out + escapeHtml(line.slice(pos));
}

/* --- views ---------------------------------------------------------------- */

// Workspace state. One screen, no routes: lane tabs and the function list
// drive which pass/function the split view and bottom panel show.
let STATE = {
  lane: "ir",                // "ir" | "mir" — active lane tab
  passId: null,              // selected pass id (manifest)
  fn: null,                  // selected function name
  mode: "diff",              // main view: "cfg" | "diff" | "ir" (not the lane)
  orientation: "side",       // "side" (side by side) | "stack" (stacked)
  cfgSource: "after",        // CFG pane source: before | after | both
  diffContext: "hunks",      // diff pane: "hunks" (3 lines) | "full" function
  srcFile: null,             // Source view: path of the file shown
  srcLine: null,             // Source view: correlated source line, or null
  bottomOpen: true,
  bottomTab: null,
  splitRatio: 0.5,           // first pane share of the split area
  lastMode: "diff",          // last non-structure detail mode — restored when drilling in
};
let CURRENT_MANIFEST = null;   // manifest.json
let CURRENT_PASS = null;       // loaded chunk for STATE.passId

function passSummaries() { return (CURRENT_MANIFEST || { passes: [] }).passes; }
function currentPassSummary() { return passSummaries().find(p => p.id === STATE.passId); }

// --- input cards (cli/main.py build_input_pass) ---
// Each lane opens on the LLVM IR module it was handed: clang's output for the
// IR lane, the post-opt module llc reads for the machine lane. Nothing in the
// lane precedes them, so there is nothing to diff against, and their one
// "function" is a whole module, so a CFG of it is meaningless. Only IR and
// Source are offered there.
const INPUT_MODES = ["ir", "src"];
// Views that are always available regardless of the selected pass — the structure view
// overview is meaningful even on the input cards, so it is never hidden.
const GLOBAL_MODES = ["structure"];

// True on either lane's input card (lane A's "Input IR", lane B's "Optimized
// IR"): both hold a whole LLVM IR module handed to that lane, not a pass.
function isInputCard() {
  const summary = currentPassSummary();
  return !!(summary && summary.isInput);
}

function modeAvailable(mode) {
  if (GLOBAL_MODES.includes(mode)) return true;
  return !isInputCard() || INPUT_MODES.includes(mode);
}

// STATE.mode is what the user asked for and is left alone; this is what the
// current pass can actually show. Stepping onto the input card falls back to
// IR, and stepping off it restores the mode they had chosen.
function effectiveMode() {
  return modeAvailable(STATE.mode) ? STATE.mode : "ir";
}

function fnNames() {
  const s = currentPassSummary();
  return (s && s.functions) || [];
}
function fnChange(fn) {
  return CURRENT_PASS && CURRENT_PASS.functions && CURRENT_PASS.functions[fn];
}
function sourceFiles() {
  return ((CURRENT_MANIFEST || {}).metadata || {}).sourceFiles || [];
}

/* --- rail: pass list ------------------------------------------------------ */

function renderLaneTabs() {
  document.querySelectorAll("#passPanel .ptab").forEach(b =>
    b.classList.toggle("active", b.dataset.lane === STATE.lane));
}

// --- "only changed" (both lanes) ---
// "changed" means the same thing in either lane -- this pass's snapshot of some
// function differs from the previous snapshot of that function (cli/diff.py
// FnChange.changed) -- so the filter applies to machine passes exactly as it
// does to IR passes. Two things survive it: a custom pass, because the point of
// badging one is to be able to find it and an analysis-only plugin never
// changes IR, and each lane's input card, which carries changed=true.
function passVisible(p, onlyChanged) {
  return !onlyChanged || p.changed || p.isCustom;
}

// Lines added / removed across every function the pass touched (cli/emit.py
// _line_delta). This is the one per-pass number both lanes can show: llc's
// legacy pass manager reports no analyses, so the machine rows used to read
// "+0 -0" for every pass. Input cards carry no delta -- nothing precedes them.
function lineDeltaHtml(p) {
  const d = p.lineDelta;
  if (!d) return '<span class="stat"></span>';
  return `<span class="stat" title="lines added / removed">` +
    `<span class="plus">+${d.added}</span> <span class="minus">−${d.removed}</span></span>`;
}

function renderPassList() {
  const onlyChanged = document.getElementById("changedOnly").checked;
  const passes = passSummaries().filter(p =>
    p.lane === STATE.lane && passVisible(p, onlyChanged));
  const rows = passes.map(p => {
    const idx = String(p.runIndex).padStart(3, "0");
    return `
      <div class="row ${p.changed || p.isCustom ? "" : "dim"} ${p.id === STATE.passId ? "sel" : ""}" data-id="${p.id}">
        <span class="idx">#${idx}</span>
        <span class="name">${escapeHtml(p.name)}</span>
        ${p.isCustom ? '<span class="badge" title="custom pass (--custom-pass)">custom</span>' : ""}
        ${p.changed ? '<span class="dot" title="changed IR"></span>' : ""}
        ${p.spillCount ? `<span class="warn" title="${p.spillCount} spills">⚠${p.spillCount}</span>` : ""}
        ${lineDeltaHtml(p)}
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
      const { del, add } = diffStat(ch.before, ch.after);
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
  if (STATE.mode === "structure") {
    const n = passSummaries().length;
    document.getElementById("ctx").innerHTML =
      `pipeline · <span class="fn">${n}</span> passes`;
    return;
  }
  const s = currentPassSummary();
  document.getElementById("ctx").innerHTML = s
    ? `${escapeHtml(s.name)} · <span class="fn">#${String(s.runIndex).padStart(3, "0")}</span>`
      + (s.isCustom ? ' · <span class="badge">custom</span>' : "")
      + (STATE.fn ? ` · fn <span class="fn">${escapeHtml(STATE.fn)}</span>` : "")
    : "no pass selected";
}

function updateViewHead() {
  const set = (el, on) => { el.classList.toggle("on", on); el.classList.toggle("off", !on); };
  const mode = effectiveMode();
  // A view this pass cannot show is removed from the row, not dimmed: there
  // is nothing there to reason about.
  document.querySelectorAll("#modeCtl .chip").forEach(b => {
    const ok = modeAvailable(b.dataset.mode);
    set(b, ok && b.dataset.mode === mode);
    b.hidden = !ok;
  });
  // Orientation only applies where the view actually rendered a pair (the IR
  // and Source views, and CFG showing both graphs) -- ask the DOM rather than
  // re-deriving it per mode, which also covers panes that fell back to an
  // empty state.
  const paired = !!document.querySelector("#split .irpair, #split .cfg-pair");
  document.getElementById("splitCtl").classList.toggle("inactive", !paired);
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
  const stacked = STATE.orientation === "stack" ? " stacked" : "";
  const body = STATE.cfgSource === "both"
    ? `<div class="cfg-pair${stacked}">${cfgBodyHtml(dotBefore, "before")}${cfgBodyHtml(dotAfter, "after")}</div>`
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
  const { ops, del, add } = diffStat(ch.before, ch.after);
  const full = STATE.diffContext === "full";
  const hunks = diffHunks(ops, full ? Infinity : DIFF_CONTEXT);
  const chips = ["hunks", "full"].map(v =>
    `<button class="ptab ${v === STATE.diffContext ? "active" : ""}" data-ctx="${v}">${v}</button>`
  ).join("");
  const stat = `<span class="minus">−${del}</span> <span class="plus">+${add}</span> · ${escapeHtml(STATE.fn)}`;
  const fn = escapeHtml(STATE.fn);
  // .ubody spans the widest row, so every bar and row tint reaches the full
  // scroll width; the sticky bits inside slide against it.
  const body = `
    <div class="udiff-wrap">
      <div class="udiff">
        <div class="ubody">
          <div class="ufile"><span class="usticky">
            <span class="uf-a">--- before/${fn}</span>
            <span class="uf-b">+++ after/${fn}</span>
          </span></div>
          ${unifiedDiffHtml(hunks)}
        </div>
      </div>
    </div>`;
  return pane("DIFF", chips, stat, body);
}

// The whole IR, both sides, nothing collapsed -- the Diff view answers "what
// did this pass touch", this one answers "what does the function look like".
// An unchanged function still renders: both sides, no tint.
function irPaneHtml() {
  const ch = fnChange(STATE.fn);
  if (!ch) return pane("IR", "", "", '<div class="cfg-empty">(select a function)</div>');
  const { ops, del, add } = diffStat(ch.before, ch.after);
  const side = (label, mark, count, empty) => `
    <div class="irside">
      <div class="irside-head">
        <span>${label}</span>
        <span class="count ${mark === "+" ? "plus" : "minus"}">${
          count ? (mark === "+" ? "+" : "−") + count : "—"}</span>
      </div>
      <div class="udiff"><div class="ubody">${
        irSideHtml(ops, mark) || `<div class="cfg-empty">${empty}</div>`}</div></div>
    </div>`;
  const stat = ch.changed
    ? `<span class="minus">−${del}</span> <span class="plus">+${add}</span> · ${escapeHtml(STATE.fn)}`
    : `unchanged · ${escapeHtml(STATE.fn)}`;
  const body = `
    <div class="irpair${STATE.orientation === "stack" ? " stacked" : ""}">
      ${side("before", "-", del, "(no prior snapshot)")}
      <div class="divider" title="drag to resize"></div>
      ${side("after", "+", add, "(empty)")}
    </div>`;
  return pane("IR", "", stat, body);
}

// --- Source view: this stage's IR beside the C it came from -----------------

// Debug info maps each IR/MIR line to one source line (cli/sourcemap.py
// resolves the !dbg metadata at build time). The two panes are keyed on that
// line number: clicking either side highlights every counterpart of it.

// Which file this snapshot mostly came from — inlining can pull in several.
function srcFileTally(map) {
  const tally = new Map();
  for (const ref of map) {
    if (ref) tally.set(ref[0], (tally.get(ref[0]) || 0) + 1);
  }
  return [...tally.entries()].sort((a, b) => b[1] - a[1]);
}

function srcPaneHtml() {
  const empty = (note) => pane("SOURCE", "", "", `<div class="cfg-empty">${note}</div>`);
  const files = sourceFiles();
  if (!files.length) {
    return empty("(no source correlation in this report — it needs a source built "
      + "with debug info, and is skipped by --no-source-map)");
  }
  const ch = fnChange(STATE.fn);
  if (!ch) return empty("(select a function)");
  const map = ch.srcAfter || [];
  const lines = splitLines(ch.after);
  const tally = srcFileTally(map);
  if (!tally.length) {
    return empty(`(no mapped lines for ${escapeHtml(STATE.fn)} at this pass)`);
  }
  let index = files.findIndex(f => f.path === STATE.srcFile);
  if (index < 0 || !tally.some(([i]) => i === index)) index = tally[0][0];
  STATE.srcFile = files[index].path;

  const irRows = lines.map((text, i) => {
    const ref = map[i];
    const on = ref && ref[0] === index;
    return `<div class="urow ${on ? "smap" : "uctx"}"${on ? ` data-ln="${ref[1]}"` : ""}>`
      + `<span class="uln">${i + 1}</span>`
      + `<span class="uln sln">${on ? ref[1] : ""}</span>`
      + `<code class="utext">${highlightIR(text) || "&nbsp;"}</code>`
      + "</div>";
  }).join("");

  const covered = new Set(map.filter(r => r && r[0] === index).map(r => r[1]));
  const srcRows = splitLines(files[index].text).map((text, i) => {
    const line = i + 1;
    return `<div class="urow crow${covered.has(line) ? " cmap" : ""}" data-ln="${line}">`
      + `<span class="uln">${line}</span>`
      + `<code class="utext">${highlightC(text) || "&nbsp;"}</code>`
      + "</div>";
  }).join("");

  // Only worth a file switcher when this snapshot really spans several files.
  const chips = tally.length > 1 ? tally.map(([i, n]) =>
    `<button class="ptab ${i === index ? "active" : ""}" data-srcfile="${escapeHtml(files[i].path)}"
      title="${n} mapped lines">${escapeHtml(files[i].name)}</button>`).join("") : "";
  const mapped = map.filter(r => r && r[0] === index).length;
  const stat = `${mapped}/${lines.length} lines mapped · ${escapeHtml(files[index].name)}`;
  const side = (label, body, cls) => `
    <div class="irside ${cls}">
      <div class="irside-head"><span>${label}</span></div>
      <div class="udiff"><div class="ubody">${body}</div></div>
    </div>`;
  const body = `
    <div class="irpair${STATE.orientation === "stack" ? " stacked" : ""}">
      ${side(CURRENT_PASS && CURRENT_PASS.lane === "mir" && !isInputCard()
              ? "machine ir" : "llvm ir", irRows, "irmap")}
      <div class="divider" title="drag to resize"></div>
      ${side(files[index].name, srcRows, "cmapside")}
    </div>`;
  return pane("SOURCE", chips, stat, body);
}

// --- Structure tree: the pass-manager hierarchy at a glance ---------------------

// A leaf row for a machine/ir pass, reusing the pass list's signal language.
// Looks the pass's summary up by id so it can show timing, badges and the
// analysis count without duplicating that data in the tree.
function pipelineLeafHtml(summary) {
  if (!summary) return "";
  const a = summary.analysisCounts || {};
  const idx = String(summary.runIndex).padStart(3, "0");
  const cls = [];
  if (summary.id === STATE.passId) cls.push("sel");
  if (summary.isCustom) cls.push("custom");
  else if (!summary.changed) cls.push("dim");
  return `
    <div class="ptree-leaf ${cls.join(" ")}" data-id="${summary.id}">
      <span class="ptree-idx">#${idx}</span>
      <span class="ptree-name">${escapeHtml(summary.name)}</span>
      <span class="ptree-badges">
        ${summary.isCustom ? '<span class="badge" title="custom pass (--custom-pass)">custom</span>' : ""}
        ${summary.changed ? '<span class="dot" title="changed IR"></span>' : ""}
        ${summary.spillCount ? `<span class="warn" title="${summary.spillCount} spills">⚠${summary.spillCount}</span>` : ""}
      </span>
      <span class="ptree-stat" title="analyses run / invalidated">+${a.run || 0} −${a.invalidated || 0}</span>
      <span class="ptree-time">${summary.timeMs != null ? summary.timeMs.toFixed(2) + " ms" : ""}</span>
    </div>`;
}

// Recursively render a tree node. Manager/group nodes are collapsible headers;
// pass leaves render as selectable rows. Returns { html, leafCount } so a group
// can show how many real passes it contains.
function pipelineNodeHtml(node, summariesById, onlyChanged, depth) {
  const children = node.children || [];
  if (!children.length) {
    // Leaf.
    const summary = node.passId != null ? summariesById.get(node.passId) : null;
    if (node.kind !== "pass" || node.passId == null || !summary) {
      return { html: "", leaves: 0 };
    }
    if (onlyChanged && !summary.changed && !summary.isCustom) {
      return { html: "", leaves: 0 };
    }
    return { html: pipelineLeafHtml(summary), leaves: 1 };
  }

  // Container: recurse into children first so we know the leaf count, then
  // decide whether the whole group is filtered out.
  const parts = [];
  let leaves = 0;
  for (const child of children) {
    const r = pipelineNodeHtml(child, summariesById, onlyChanged, depth + 1);
    if (r.html) parts.push(r.html);
    leaves += r.leaves;
  }
  if (!parts.length) return { html: "", leaves: 0 };

  const caret = `<span class="ptree-caret"></span>`;
  const count = leaves === 1 ? "1 pass" : `${leaves} passes`;
  const inner = parts.join("");
  const html = `
    <div class="ptree-group" data-depth="${depth}">
      <div class="ptree-head" style="padding-left:${depth * 14}px">
        ${caret}<span class="ptree-gname">${escapeHtml(node.name)}</span>
        <span class="ptree-gcount">${count}</span>
      </div>
      <div class="ptree-children">${inner}</div>
    </div>`;
  return { html, leaves };
}

function pipelineTreeHtml() {
  const manifest = CURRENT_MANIFEST || { metadata: {}, passes: [] };
  const tree = manifest.metadata.pipelineTree;
  const summariesById = new Map(manifest.passes.map(p => [p.id, p]));
  const totalPasses = manifest.passes.length;
  if (!tree || (!tree.ir && !tree.mir)) {
    return pane("STRUCTURE", "", "",
      '<div class="cfg-empty">(no no structure captured)</div>');
  }

  const onlyChanged = document.getElementById("changedOnly").checked;
  const lanes = [
    { key: "ir", label: "IR" },
    { key: "mir", label: "machine" },
  ];
  let body = "";
  let shown = 0;
  for (const { key, label } of lanes) {
    const root = tree[key];
    if (!root) continue;
    const { html, leaves } = pipelineNodeHtml(root, summariesById, onlyChanged, 0);
    if (!html) continue;
    shown += leaves;
    body += `
      <div class="ptree-lane">
        <div class="ptree-lane-label">${label}</div>
        <div class="ptree">${html}</div>
      </div>`;
  }

  if (!body) {
    return pane("STRUCTURE", "", "",
      '<div class="cfg-empty">(no passes match the current filter)</div>');
  }
  const stat = `${totalPasses} passes · ${shown} shown`;
  return pane("STRUCTURE", "", stat, `<div class="ptree-wrap">${body}</div>`);
}

// Drill from a tree leaf into its pass: switch to the pass's lane, restore the
// last detail view, and select it (loads the chunk and renders the detail pane).
function selectPassFromOverview(id) {
  const p = passSummaries().find(x => x.id === id);
  if (!p) return;
  STATE.lane = p.lane;
  STATE.mode = STATE.lastMode;
  renderLaneTabs();
  selectPass(id);
}

// Highlight one source line on both sides at once. Done by class toggle rather
// than a re-render so neither pane loses its scroll position.
function applySrcHighlight(scrollTo) {
  const line = STATE.srcLine == null ? null : String(STATE.srcLine);
  document.querySelectorAll("#split .urow[data-ln]").forEach(row =>
    row.classList.toggle("hit", line !== null && row.dataset.ln === line));
  if (!scrollTo) return;
  const target = document.querySelector(`#split .${scrollTo} .urow.hit`);
  if (target) scrollRowIntoView(target);
}

function scrollRowIntoView(row) {
  const scroller = row.closest(".udiff");
  if (!scroller) return;
  const top = row.offsetTop - scroller.clientHeight / 2 + row.offsetHeight / 2;
  scroller.scrollTop = Math.max(0, top);
}

function renderMain() {
  renderCtx();
  destroyCfgGraphs();
  const split = document.getElementById("split");
  const mode = effectiveMode();
  split.innerHTML =
    mode === "structure" ? pipelineTreeHtml()
      : mode === "cfg" ? cfgPaneHtml()
        : mode === "diff" ? diffPaneHtml()
          : mode === "src" ? srcPaneHtml()
            : irPaneHtml();
  if (mode === "src") applySrcHighlight("cmapside");
  const first = split.querySelector(".irpair > .irside");
  if (first) first.style.flex = `0 0 ${(STATE.splitRatio * 100).toFixed(1)}%`;
  mountCfgGraphs();
  updateViewHead();  // after the panes: it reads what they rendered
}

/* --- bottom panel ---------------------------------------------------------- */

function bottomTabs() {
  // An input card ran no analyses and allocated no registers; only its Log,
  // which says where the module came from, has anything to show.
  if (isInputCard()) return ["Log"];
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

// cli/cfg.py emits each node as: n0 [name="bb.0", label="…", code="…"], with
// label and code absent on a block with no instructions. Attributes are read
// by name rather than by position, so their order stays cli/cfg.py's business.
const DOT_ATTR_RE = /(\w+)="((?:[^"\\]|\\.)*)"/g;

function parseDot(dot) {
  const nodes = [], edges = [];
  for (const line of String(dot || "").split("\n")) {
    let m = line.match(/^\s*n(\d+) \[(.*)\]\s*;?$/);
    if (m) {
      const un = (s) => s.replace(/\\n/g, "\n").replace(/\\(.)/g, "$1");
      const attrs = {};
      for (const a of m[2].matchAll(DOT_ATTR_RE)) attrs[a[1]] = un(a[2]);
      const label = attrs.label || "";
      // The block name is not drawn in the graph -- a bare "6" or "bb.1" among
      // the instructions reads as one of them -- only in the block detail. A
      // report written before the name attribute existed carries it as the
      // label's first line; drop that line so those graphs render the same.
      nodes.push(attrs.name !== undefined
        ? { id: +m[1], name: attrs.name, label, code: attrs.code || "" }
        : {
            id: +m[1],
            name: label.split("\n")[0],
            label: label.split("\n").slice(1).join("\n"),
            code: attrs.code || "",
          });
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
        name: nd.name,
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

/* --- command sheet --------------------------------------------------------- */
// metadata.commands is the argv of every stage that actually ran (cli/main.py
// build_commands), instrumentation flags and all. It answers "what exactly
// produced this report" -- which clang, which pipeline string, which triple,
// which plugin .so -- for someone reading the report on another machine.

function renderCommands(manifest) {
  const commands = (manifest.metadata || {}).commands || [];
  const body = document.getElementById("cmdBody");
  const button = document.getElementById("cmdBtn");
  // Older reports carry no commands; the button would open an empty sheet.
  if (!commands.length) { button.hidden = true; return; }
  body.innerHTML = commands.map((c, i) => `
    <div class="cmdrow">
      <div class="cmdrow-head">
        <span class="cmdstage">${escapeHtml(c.stage || "")}</span>
        <span class="cmdnote">${escapeHtml(c.note || "")}</span>
        <button class="chip cmdcopy" data-cmd="${i}">copy</button>
      </div>
      <pre class="cmdline">${escapeHtml(c.line || (c.argv || []).join(" "))}</pre>
    </div>`).join("");
}

// Reports open from file://, where navigator.clipboard is unavailable in some
// browsers; fall back to the selection-based copy, which works everywhere.
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(() => legacyCopy(text));
  }
  return Promise.resolve(legacyCopy(text));
}

function legacyCopy(text) {
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.cssText = "position:fixed;top:-1000px;opacity:0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); } catch (e) { /* nothing else to try */ }
  area.remove();
}

function toggleCommands(open) {
  const sheet = document.getElementById("cmdSheet");
  const button = document.getElementById("cmdBtn");
  const show = open === undefined ? sheet.hidden : open;
  sheet.hidden = !show;
  button.classList.toggle("on", show);
  button.setAttribute("aria-expanded", String(show));
}

function renderMeta(manifest) {
  const m = manifest.metadata;
  // opt, llc and llvm-dis all report the same "LLVM version X" string, so
  // list each distinct version once instead of repeating it per tool.
  const tools = [...new Set(Object.values(m.toolVersions || {}).map(v =>
    v.replace(/\s*\(.*\)$/, "")))].join(" · ");
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
  renderCommands(manifest);
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

document.getElementById("cmdBtn").addEventListener("click", () => toggleCommands());
document.getElementById("cmdClose").addEventListener("click", () => toggleCommands(false));
document.getElementById("cmdBody").addEventListener("click", evt => {
  const b = evt.target.closest(".cmdcopy");
  if (!b) return;
  const line = b.closest(".cmdrow").querySelector(".cmdline").textContent;
  copyText(line);
  b.textContent = "copied";
  setTimeout(() => { b.textContent = "copy"; }, 1200);
});
document.addEventListener("keydown", evt => {
  if (evt.key === "Escape") toggleCommands(false);
});

document.getElementById("modeCtl").addEventListener("click", evt => {
  const b = evt.target.closest(".chip[data-mode]");
  if (!b || !modeAvailable(b.dataset.mode)) return;
  // Remember the last detail-oriented mode so clicking a structure leaf can
  // restore it; the overview itself is not a "last mode" we would restore.
  if (b.dataset.mode !== "structure") STATE.lastMode = b.dataset.mode;
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

// Pane chips live inside the rebuilt panes: CFG source (before / after /
// both), diff context (hunks / full) and the Source view's file switcher.
document.getElementById("split").addEventListener("click", evt => {
  const src = evt.target.closest(".ptab[data-src]");
  if (src) { STATE.cfgSource = src.dataset.src; renderMain(); return; }
  const ctx = evt.target.closest(".ptab[data-ctx]");
  if (ctx) { STATE.diffContext = ctx.dataset.ctx; renderMain(); return; }
  const file = evt.target.closest(".ptab[data-srcfile]");
  if (file) { STATE.srcFile = file.dataset.srcfile; renderMain(); return; }

  // Structure tree: a group header toggles collapse; a leaf drills into its pass.
  const head = evt.target.closest(".ptree-head");
  if (head && STATE.mode === "structure") {
    const group = head.closest(".ptree-group");
    group.classList.toggle("collapsed");
    return;
  }
  const leaf = evt.target.closest(".ptree-leaf[data-id]");
  if (leaf && STATE.mode === "structure") {
    selectPassFromOverview(+leaf.dataset.id);
    return;
  }

  // Source view: clicking either side selects that source line on both.
  // Clicking the row that is already selected clears the correlation.
  const row = evt.target.closest("#split .urow[data-ln]");
  if (!row || STATE.mode !== "src") return;
  const line = +row.dataset.ln;
  STATE.srcLine = STATE.srcLine === line ? null : line;
  // Scroll the *other* pane: the side you clicked is already where you want it.
  applySrcHighlight(row.closest(".cmapside") ? "irmap" : "cmapside");
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

// Divider drag: the element just before the divider takes the space, in
// whichever container holds it (today the IR view's before/after pair).
{
  const splitEl = document.getElementById("split");
  splitEl.addEventListener("pointerdown", evt => {
    const divider = evt.target.closest(".divider");
    if (!divider) return;
    const container = divider.parentElement;
    const first = divider.previousElementSibling;
    if (!container || !first) return;
    const stacked = container.classList.contains("stacked");
    const rect = container.getBoundingClientRect();
    const total = stacked ? rect.height : rect.width;
    const startPos = stacked ? evt.clientY : evt.clientX;
    const startSize = stacked
      ? first.getBoundingClientRect().height
      : first.getBoundingClientRect().width;
    // Keep the ratio in a variable: re-reading it out of style.flex picks up
    // the "0" of the "0 0 62.5%" shorthand, not the basis.
    let ratio = STATE.splitRatio;
    const move = e => {
      const delta = (stacked ? e.clientY : e.clientX) - startPos;
      ratio = Math.min(0.85, Math.max(0.15, (startSize + delta) / total));
      first.style.flex = `0 0 ${(ratio * 100).toFixed(1)}%`;
    };
    const up = () => {
      STATE.splitRatio = ratio;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      resizeGraphs();  // graphs track the new pane size
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

document.getElementById("changedOnly").addEventListener("change", () => {
  renderPassList();
  // The structure view shares the same "only changed" filter as the pass
  // list, so re-render it too when it is the active view.
  if (STATE.mode === "structure") renderMain();
});
document.getElementById("fnFilter").addEventListener("input", renderFnList);

window.addEventListener("DOMContentLoaded", boot);
