/* LLVM-Lens report viewer: rail, views, CFG, drawer. */

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

const DIFF_CONTEXT = 3;   // unchanged lines kept around a change (git default)

function splitLines(text) {
  if (!text) return [];
  return String(text).replace(/\n$/, "").split("\n");
}

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

function diffOps(beforeText, afterText) {
  const A = splitLines(beforeText), B = splitLines(afterText);

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

function unifiedDiffHtml(hunks) {
  return hunks.map(hunk => {
    const hidden = hunk.hidden
      ? `<span class="uskip">${hunk.hidden} unchanged `
        + `line${hunk.hidden === 1 ? "" : "s"} hidden</span>`
      : "";
    const rows = hunk.rows.map(row => {
      const cls = row.op === "+" ? "add" : row.op === "-" ? "del" : "uctx";
      const body = highlightIR(row.text);
      return `<div class="urow ${cls}" data-a="${row.a || ""}"`
        + ` data-b="${row.b || ""}" data-side="${row.op === " " ? "+" : row.op}">`
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

function irSideHtml(ops, mark) {
  return ops.filter(o => o.op === " " || o.op === mark).map(o => {
    const cls = o.op === " " ? "uctx" : (mark === "+" ? "add" : "del");
    return `<div class="urow ${cls}" data-a="${o.a || ""}"`
      + ` data-b="${o.b || ""}" data-side="${mark}">`
      + `<span class="uln">${mark === "+" ? o.b : o.a}</span>`
      + `<code class="utext">${highlightIR(o.text) || "&nbsp;"}</code>`
      + "</div>";
  }).join("");
}

/* --- blame: which pass put each line of IR here ---------------------------- */

const BLAME_TINTS = 8;   // distinct gutter tints before they start repeating

// One lane's lineage document: pass names, writes and per-line chains, interned.
const blameDocs = new Map();   // lane -> Promise<doc|null>

function loadBlame(lane) {
  if (!blameDocs.has(lane)) {
    blameDocs.set(lane, loadJSON(`blame-${lane}`).catch(() => null));
  }
  return blameDocs.get(lane);
}

// A stored chain id -> the writes behind it, oldest first.
function blameChain(doc, id) {
  return (doc.hist[id] || []).map(e => {
    const [run, name, kind] = doc.events[e];
    return { run, name: doc.names[name], kind: doc.kinds[kind] };
  });
}

// lane -> Map(run -> card id), so a chain row can jump to its pass.
const blameIds = new Map();

async function blameIdsFor(lane) {
  if (!blameIds.has(lane)) {
    const manifest = await manifestPromise;
    blameIds.set(lane, new Map(manifest.passes
      .filter(p => p.lane === lane && !p.isInput)
      .map(p => [p.runIndex, p.id])));
  }
  return blameIds.get(lane);
}

// The lineage of one function: a state per run that changed it, oldest first,
// each with its per-line chains. Only the last state ships its text; a row on
// screen lends the text to any earlier one.
function blameWalkFor(doc, fn) {
  const entry = doc && doc.functions ? doc.functions[fn] : null;
  if (!entry || !entry.states.length) return null;
  const runs = entry.states.map(s => ({
    run: s.run, lines: null, history: s.h.map(id => blameChain(doc, id)),
  }));
  const last = runs[runs.length - 1];
  last.lines = entry.text;
  return { runs, lines: entry.text, history: last.history };
}

// The function as of *run*: the last state at or before it.
function blameAt(walk, run) {
  let found = walk.runs[0];
  for (const state of walk.runs) if (state.run <= run) found = state;
  return found;
}

function blameLast(history) {
  return history && history.length ? history[history.length - 1] : null;
}

function blameTint(run) {
  return run == null ? "btin" : `bt${run % BLAME_TINTS}`;
}

// The line a rendered IR row shows, as plain text.
function rowText(row) {
  const code = row.querySelector(".utext");
  const text = code ? code.textContent : "";
  return text === "\u00a0" ? "" : text;
}

const blameCache = new Map();   // "lane:fn" -> walk
let BLAME = null;               // the walk on screen: {key, walk, ids, failed}

// One document per lane covers every function, so this resolves at most once.
function ensureBlame(lane, fn) {
  if (!fn) return;
  const key = `${lane}:${fn}`;
  if (BLAME && BLAME.key === key) return;
  const cached = blameCache.get(key);
  BLAME = { key, walk: cached || null, ids: blameIds.get(lane) || null, failed: false };
  if (cached) return;
  Promise.all([loadBlame(lane), blameIdsFor(lane)]).then(([doc, ids]) => {
    const walk = blameWalkFor(doc, fn);
    if (!walk) throw new Error("no lineage for this function");
    blameCache.set(key, walk);
    if (!BLAME || BLAME.key !== key) return;
    BLAME.walk = walk;
    BLAME.ids = ids;
    renderMain();
  }).catch(() => {
    if (!BLAME || BLAME.key !== key) return;
    BLAME.failed = true;
    renderMain();
  });
}

// The row the inspector describes, in whichever view is on screen.
function blameEntry() {
  const walk = BLAME && BLAME.walk;
  const line = STATE.blameLine;
  if (!walk || !line) return null;
  if (STATE.mode === "blame") {
    if (line > walk.lines.length) return null;
    return { run: walk.runs[walk.runs.length - 1].run, text: walk.lines[line - 1],
             history: walk.history[line - 1] || [] };
  }
  const summary = currentPassSummary();
  if (!summary || !STATE.fn) return null;
  const after = blameAt(walk, summary.runIndex);
  let state = after;
  if (STATE.blameSide === "-") {
    const i = walk.runs.indexOf(after);
    state = i > 0 ? walk.runs[i - 1] : walk.runs[0];
  }
  if (line > state.history.length) return null;
  // The row that was clicked is the line as it stood in this state.
  return { run: state.run, text: STATE.blameText || "", history: state.history[line - 1] || [] };
}

function blameInspectorHtml() {
  if (STATE.blameLine == null) return "";
  if (!BLAME || !BLAME.walk) {
    const note = BLAME && BLAME.failed
      ? "no lineage recorded for this function in this report"
      : "reading the pass lineage…";
    return `<div class="bdetail bwait">${escapeHtml(note)}</div>`;
  }
  const entry = blameEntry();
  if (!entry) return "";
  const ids = BLAME.ids || new Map();
  const rows = entry.history.length
    ? entry.history.map(ev => {
        const id = ids.get(ev.run);
        return `
        <div class="brow${id == null ? " bnone" : ""}"`
          + `${id == null ? "" : ` data-goto="${id}"`} title="run ${ev.run}">`
          + `<span class="brun">${ev.run}</span>`
          + `<span class="bname">${escapeHtml(ev.name)}</span>`
          + `<span class="bkind ${ev.kind}">${ev.kind}</span>`
          + "</div>";
      }).join("")
    : '<div class="brow bnone">untouched since the input IR</div>';
  return `
    <div class="bdetail">
      <div class="bdetail-head">
        <span class="btitle">line ${STATE.blameLine}</span>
        <code class="bdetail-code">${highlightIR(entry.text) || "&nbsp;"}</code>
        <span class="bclose" data-bclose="1" title="close">✕</span>
      </div>
      <div class="bchain">${rows}</div>
    </div>`;
}

function blameRowsHtml(walk) {
  return walk.lines.map((text, i) => {
    const last = blameLast(walk.history[i]);
    const who = last
      ? `run ${last.run} · ${last.name} · ${last.kind}`
      : "already in the input IR";
    return `<div class="urow bline${STATE.blameLine === i + 1 ? " hit" : ""}"`
      + ` data-blame="${i + 1}" title="${escapeHtml(who)}">`
      + `<span class="uln">${i + 1}</span>`
      + `<span class="ublame ${blameTint(last && last.run)}">${last ? last.run : "in"}</span>`
      + `<code class="utext">${highlightIR(text) || "&nbsp;"}</code>`
      + "</div>";
  }).join("");
}

function blamePaneHtml() {
  if (!STATE.fn) return pane("BLAME", "", "", '<div class="cfg-empty">(select a function)</div>');
  const walk = BLAME && BLAME.walk;
  if (!walk) {
    const note = BLAME && BLAME.failed
      ? "(no lineage recorded for this function in this report)"
      : "(reading the pass lineage…)";
    return pane("BLAME", "", "", `<div class="cfg-empty">${escapeHtml(note)}</div>`);
  }
  const writers = new Set();
  for (const h of walk.history) {
    const last = blameLast(h);
    if (last) writers.add(last.run);
  }
  const stat = `${walk.lines.length} lines · ${writers.size} writer`
    + `${writers.size === 1 ? "" : "s"} · ${escapeHtml(STATE.fn)}`;
  const fn = escapeHtml(STATE.fn);
  const body = `
    <div class="udiff-wrap">
      <div class="udiff">
        <div class="ubody">
          <div class="ufile"><span class="usticky">
            <span class="uf-a">--- blame/${fn}</span>
            <span class="uf-b">+++ final/${fn}</span>
          </span></div>
          ${blameRowsHtml(walk)}
        </div>
      </div>
    </div>
    ${blameInspectorHtml()}`;
  return pane("BLAME", "", stat, body);
}

/* --- llvm ir highlighting -------------------------------------------------- */

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

function isMirDebugLine(line) {
  return /^\s*(DBG_(VALUE(_LIST)?|INSTR_REF|PHI)|(frame-(setup|destroy)\s+)?CFI_INSTRUCTION|; predecessors:)/.test(line);
}

function cleanMirLine(line) {
  line = line.replace(/^(\d+B)\s+/, "  ");
  line = line.replace(/,?\s*debug-instr-number\s+\d+/, "");
  line = line.replace(/,?\s*debug-location\s+!\d+/, "");
  line = line.replace(/\s*; [^;]*\.\w+:\d+(?::\d+)?( line no:\d+)?\s*$/, "");
  line = line.replace(/,?\s*!dbg !\d+/, "");
  return line;
}

function isIrDebugLine(line) {
  return /^\s*(#dbg_(declare|value)|call void @llvm\.dbg\.(declare|value))/.test(line);
}

function dropDebugLine(line) {
  return isMirDebugLine(line) || isIrDebugLine(line);
}

function highlightIR(text) {
  return String(text).split("\n").map(line => {
    if (!line.trim()) return "";
    line = cleanMirLine(line);
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

let STATE = {
  lane: "ir",                // "ir" | "mir" — active lane tab
  passId: null,              // selected pass id (manifest)
  fn: null,                  // selected function name
  mode: "diff",              // main view: "cfg" | "diff" | "ir" | "blame" (not the lane)
  orientation: "side",       // "side" (side by side) | "stack" (stacked)
  cfgSource: "after",        // CFG pane source: before | after | both
  analysisTypes: ["pdt"],    // analyses pane: any of pdt | cdg | ddg | pdg | mdg | lnt | cg
  diffContext: "hunks",      // diff pane: "hunks" (3 lines) | "full" function
  srcFile: null,             // Source view: path of the file shown
  srcLine: null,             // Source view: correlated source line, or null
  iselBlock: null,           // ISel view: correlated block pair, or null
  iselRefs: [],              // ISel view: IR lines a machine row names
  blameLine: null,           // Blame/inspector: the IR line being attributed
  blameSide: "+",            // which side of the diff that line number counts on
  blameText: "",             // that line's text, as the clicked row rendered it
  bottomOpen: true,
  bottomTab: null,
  splitRatio: 0.5,           // first pane share of the split area
  lastMode: "diff",          // last overview detail mode — restored when drilling in
  pipeBoth: true,            // Flow: draw both lanes, or just the selected one
};
let CURRENT_MANIFEST = null;   // manifest.json
let CURRENT_PASS = null;       // loaded chunk for STATE.passId

function passSummaries() { return (CURRENT_MANIFEST || { passes: [] }).passes; }
function currentPassSummary() { return passSummaries().find(p => p.id === STATE.passId); }

// --- input cards (report.build_input_pass) ---
const INPUT_MODES = ["ir", "src"];
const GLOBAL_MODES = ["structure", "analyses", "pipeline"];
// Overviews are whole-report, so drilling into a pass must not return to them.
const OVERVIEW_MODES = ["structure", "pipeline"];

function isInputCard() {
  const summary = currentPassSummary();
  return !!(summary && summary.isInput);
}

function hasIselMap() {
  const summary = currentPassSummary();
  return !!(summary && summary.iselFns && STATE.fn && summary.iselFns.includes(STATE.fn));
}

function modeAvailable(mode) {
  if (GLOBAL_MODES.includes(mode)) return true;
  if (mode === "isel") return hasIselMap();
  return !isInputCard() || INPUT_MODES.includes(mode);
}

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
function passVisible(p, onlyChanged) {
  return !onlyChanged || p.changed || p.isCustom;
}

function lanePasses() {
  return passSummaries().filter(p => p.lane === STATE.lane);
}

function listedPasses() {
  const q = (document.getElementById("passFilter").value || "").trim().toLowerCase();
  const onlyChanged = document.getElementById("changedOnly").checked;
  return lanePasses().filter(p =>
    passVisible(p, onlyChanged) && (!q || p.name.toLowerCase().includes(q)));
}

function churn(p) {
  return p.lineDelta ? p.lineDelta.added + p.lineDelta.removed : 0;
}

const BAR_W = 46;   // must match .bar in style.css

function churnBarHtml(p, max) {
  const d = p.lineDelta;
  if (!d || !churn(p)) return '<span class="bar none"></span>';
  const w = n => Math.max(n ? 1 : 0, Math.round(n / max * BAR_W));
  return `<span class="bar" title="+${d.added} −${d.removed}">` +
    `<span class="del" style="width:${w(d.removed)}px"></span>` +
    `<span class="add" style="width:${w(d.added)}px"></span></span>`;
}

function renderPassList() {
  const passes = listedPasses();
  const max = Math.max(1, ...lanePasses().map(churn));
  const rows = passes.map(p => `
      <div class="row ${p.changed || p.isCustom ? "" : "dim"} ${p.id === STATE.passId ? "sel" : ""}" data-id="${p.id}">
        <span class="idx">#${String(p.runIndex).padStart(3, "0")}</span>
        <span class="name">${escapeHtml(p.name)}</span>
        ${p.isCustom ? '<span class="badge" title="custom pass (--custom-pass)">custom</span>' : ""}
        ${p.spillCount ? `<span class="warn" title="${p.spillCount} spills">⚠${p.spillCount}</span>` : ""}
        ${churnBarHtml(p, max)}
        <span class="time" title="milliseconds">${p.timeMs != null ? p.timeMs.toFixed(2) : ""}</span>
      </div>`).join("");
  document.getElementById("passList").innerHTML = rows || `<div class="row empty">(no ${
    lanePasses().length ? "passes match the filter"
      : (STATE.lane === "ir" ? "IR" : "machine") + " passes captured"})</div>`;
  document.getElementById("passCount").textContent = passes.length;
  document.querySelectorAll("#passList .row[data-id]").forEach(r =>
    r.addEventListener("click", () => selectPass(+r.dataset.id)));
  renderSpine(max);
}

function renderSpine(max) {
  document.getElementById("spine").innerHTML = lanePasses().map(p => {
    const c = churn(p);
    const w = c ? Math.max(3, Math.round(c / max * 14)) : 2;
    const cls = ["tick", p.spillCount ? "spill" : "", p.id === STATE.passId ? "cur" : ""]
      .filter(Boolean).join(" ");
    return `<span class="${cls}" data-id="${p.id}" title="#${
      String(p.runIndex).padStart(3, "0")} ${escapeHtml(p.name)}">` +
      `<i style="width:${w}px"></i></span>`;
  }).join("");
}

async function selectPass(id) {
  STATE.passId = id;
  STATE.fn = null;
  STATE.blameLine = null;
  CURRENT_PASS = null;
  renderPassList();
  const row = document.querySelector("#passList .row.sel");
  if (row) row.scrollIntoView({ block: "nearest" });
  renderFnList();
  renderMain();
  renderBottom();
  const data = await loadPass(id);
  if (STATE.passId !== id) return;  // user switched passes while loading
  CURRENT_PASS = data;
  const names = fnNames();
  const changed = (f) => fnChange(f) && fnChange(f).changed;
  STATE.fn = names.find(f => f !== "[module]" && changed(f))
    || names.find(changed) || names[0] || null;
  renderFnList();
  renderMain();
  renderBottom();
  renderCtx();
}

/* --- rail: function list --------------------------------------------------- */

const FN_GROUP_AT = 8;   // below this, a module is short enough to list flat

function fnRowHtml(n) {
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
        ${stat || '<span class="stat"></span>'}
      </div>`;
}

function renderFnList() {
  const q = ((document.getElementById("fnFilter") || {}).value || "").toLowerCase();
  const all = fnNames();
  const names = all.filter(n => !q || n.toLowerCase().includes(q));
  let rows;
  if (all.length >= FN_GROUP_AT && CURRENT_PASS) {
    const group = (label, list) => list.length
      ? `<div class="fngroup">${label}<span class="count">${list.length}</span></div>`
        + list.map(fnRowHtml).join("")
      : "";
    const changed = names.filter(n => (fnChange(n) || {}).changed);
    rows = group("changed by this pass", changed)
      + group("rest of module", names.filter(n => !(fnChange(n) || {}).changed));
  } else {
    rows = names.map(fnRowHtml).join("");
  }
  document.getElementById("fnList").innerHTML =
    rows || (all.length
      ? '<div class="row empty">(no functions match the filter)</div>'
      : '<div class="row empty">(no functions captured)</div>');
  document.getElementById("fnCount").textContent = all.length;
  document.querySelectorAll("#fnList .row[data-fn]").forEach(r =>
    r.addEventListener("click", () => selectFn(r.dataset.fn)));
}

function selectFn(name) {
  STATE.fn = name;
  STATE.blameLine = null;
  renderFnList();
  renderMain();
  renderCtx();
  renderBottom();  // the RegMap and Spills tables are per function
}

/* --- main view: context line, panes ---------------------------------------- */

function renderCtx() {
  if (STATE.mode === "structure") {
    const n = passSummaries().length;
    document.getElementById("ctx").innerHTML =
      `pipeline · <span class="fn">${n}</span> passes`;
    return;
  }
  if (STATE.mode === "pipeline") {
    const passes = passSummaries();
    const onlyChanged = document.getElementById("changedOnly").checked;
    const lanes = STATE.pipeBoth ? ["ir", "mir"] : [STATE.lane];
    const stats = lanes.map(l => pipeCollapse(passes, l, onlyChanged).stats);
    const nodes = stats.reduce((s, x) => s + x.nodes, 0);
    const changed = stats.reduce((s, x) => s + x.changed, 0);
    document.getElementById("ctx").innerHTML =
      `flow · <span class="fn">${nodes}</span> nodes · `
      + `<span class="fn">${changed}</span> changed · ${passes.length} passes`;
    return;
  }
  if (STATE.mode === "blame") {
    const walk = BLAME && BLAME.walk;
    document.getElementById("ctx").innerHTML = walk
      ? `blame · <span class="fn">${walk.runs.length - 1}</span> changes · `
        + (STATE.fn ? `fn <span class="fn">${escapeHtml(STATE.fn)}</span>` : "no function")
      : "blame · reading the lineage…";
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
  document.querySelectorAll("#modeCtl .chip").forEach(b => {
    const ok = modeAvailable(b.dataset.mode);
    set(b, ok && b.dataset.mode === mode);
    b.hidden = !ok;
  });
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

const ANALYSIS_TYPES = ["pdt", "cdg", "ddg", "pdg", "mdg", "lnt", "cg"];
const ANALYSIS_BUCKETS = ["run", "invalidated"];
const ANALYSIS_LABELS = {
  pdt: "PDT", cdg: "CDG", ddg: "DDG", pdg: "PDG", mdg: "MDG", lnt: "LNT", cg: "Call graph",
};

// A manifest or a config may name graphs we do not know; keep the ones we do,
// in the order the chips are drawn so the pane and the row never disagree.
function analysisSelection(value) {
  const wanted = Array.isArray(value) ? value : [value];
  return ANALYSIS_TYPES.filter(t => wanted.includes(t));
}

function analysesPaneHtml() {
  const analyses = ((CURRENT_MANIFEST || {}).metadata || {}).analyses || {};
  const functions = analyses.functions || {};
  if (!analyses.callGraph && !Object.keys(functions).length) {
    return pane("Graphs", "", "", '<div class="cfg-empty">(no analyses for this module)</div>');
  }
  STATE.analysisTypes = analysisSelection(STATE.analysisTypes);
  const picked = STATE.analysisTypes;
  const chips = ANALYSIS_TYPES.map(t =>
    `<button class="ptab ${picked.includes(t) ? "active" : ""}" data-analysis="${t}">${ANALYSIS_LABELS[t]}</button>`
  ).join("");
  if (!picked.length) {
    return pane("Graphs", chips, "final graphs only · not per-pass",
      '<div class="cfg-empty">(no graph selected — click a name above)</div>');
  }
  // One entry per selected graph; `cg` is the module-wide one, the rest are
  // the selected function's.
  const fnGraphs = functions[STATE.fn];
  const bodies = picked.map(t => {
    const dot = t === "cg" ? analyses.callGraph : (fnGraphs && fnGraphs[t]);
    const label = t === "cg"
      ? "call graph"
      : ANALYSIS_LABELS[t] + (STATE.fn ? ` · ${STATE.fn}` : "");
    return { dot, label, type: t };
  });
  // Nothing to draw at all reads as one message, not one per selected graph.
  const body = bodies.every(b => !b.dot)
    ? `<div class="cfg-empty">(no ${picked.map(t => ANALYSIS_LABELS[t]).join(", ")}`
      + `${picked.includes("cg") ? "" : STATE.fn ? ` for ${escapeHtml(STATE.fn)}` : ""})</div>`
    : `<div class="graph-stack">${bodies.map(b => b.dot
        ? cfgBodyHtml(b.dot, b.label)
        : `<div class="cfg-empty">(no ${ANALYSIS_LABELS[b.type]})</div>`).join("")}</div>`;
  // The graphs are a fixed whole-report set; they don't step with the pass.
  return pane("Graphs", chips, "final graphs only · not per-pass", body);
}

function diffPaneHtml() {
  const ch = fnChange(STATE.fn);
  if (!ch) return pane("DIFF", "", "", '<div class="cfg-empty">(select a function)</div>');
  if (!ch.changed) {
    return pane("DIFF", "", "",
      `<div class="cfg-empty">(${escapeHtml(STATE.fn)} unchanged — nothing to diff)</div>`);
  }
  const { ops, del, add } = diffStat(ch.before, ch.after);
  const realOps = ops.filter(o => !dropDebugLine(o.text));
  const full = STATE.diffContext === "full";
  const hunks = diffHunks(realOps, full ? Infinity : DIFF_CONTEXT);
  const chips = ["hunks", "full"].map(v =>
    `<button class="ptab ${v === STATE.diffContext ? "active" : ""}" data-ctx="${v}">${v}</button>`
  ).join("");
  const stat = `<span class="minus">−${del}</span> <span class="plus">+${add}</span> · ${escapeHtml(STATE.fn)}`;
  const fn = escapeHtml(STATE.fn);
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
    </div>
    ${blameInspectorHtml()}`;
  return pane("DIFF", chips, stat, body);
}

function irPaneHtml() {
  const ch = fnChange(STATE.fn);
  if (!ch) return pane("IR", "", "", '<div class="cfg-empty">(select a function)</div>');
  const { ops, del, add } = diffStat(ch.before, ch.after);
  const realOps = ops.filter(o => !dropDebugLine(o.text));
  const side = (label, mark, count, empty) => `
    <div class="irside">
      <div class="irside-head">
        <span>${label}</span>
        <span class="count ${mark === "+" ? "plus" : "minus"}">${
          count ? (mark === "+" ? "+" : "−") + count : "—"}</span>
      </div>
      <div class="udiff"><div class="ubody">${
        irSideHtml(realOps, mark) || `<div class="cfg-empty">${empty}</div>`}</div></div>
    </div>`;
  const stat = ch.changed
    ? `<span class="minus">−${del}</span> <span class="plus">+${add}</span> · ${escapeHtml(STATE.fn)}`
    : `unchanged · ${escapeHtml(STATE.fn)}`;
  const body = `
    <div class="irpair${STATE.orientation === "stack" ? " stacked" : ""}">
      ${side("before", "-", del, "(no prior snapshot)")}
      <div class="divider" title="drag to resize"></div>
      ${side("after", "+", add, "(empty)")}
    </div>
    ${blameInspectorHtml()}`;
  return pane("IR", "", stat, body);
}

// --- Source view: this stage's IR beside the C it came from -----------------

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
  const srcMap = ch.srcAfter || [];
  const allLines = splitLines(ch.after);
  const keep = allLines.map((t, i) => !dropDebugLine(t) && i < srcMap.length);
  const lines = allLines.filter((t, i) => keep[i]);
  const map = srcMap.filter((r, i) => keep[i]);
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
  // What the tree can hold, so "shown" can only be short of it by the filter.
  const totalPasses = manifest.passes.filter(p => !p.isInput).length;
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

function selectPassFromOverview(id) {
  const p = passSummaries().find(x => x.id === id);
  if (!p) return;
  STATE.lane = p.lane;
  STATE.mode = STATE.lastMode;
  renderLaneTabs();
  selectPass(id);
}

// The row a blame click landed on, in a diff or IR pane, reads as selected.
function applyBlameHighlight() {
  document.querySelectorAll("#split .urow.hit").forEach(row => row.classList.remove("hit"));
  const line = STATE.blameLine;
  if (line == null || (STATE.mode !== "diff" && STATE.mode !== "ir")) return;
  const attr = STATE.blameSide === "-" ? "data-a" : "data-b";
  document.querySelectorAll(`#split .urow[${attr}="${line}"]`).forEach(row => {
    if (row.dataset.side === STATE.blameSide) row.classList.add("hit");
  });
}

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
  const mode = effectiveMode();
  // The lineage document is one file per lane, so it loads on demand.
  if (mode === "blame" || STATE.blameLine != null) ensureBlame(STATE.lane, STATE.fn);
  renderCtx();
  destroyCfgGraphs();
  const split = document.getElementById("split");
  split.innerHTML =
    mode === "structure" ? pipelineTreeHtml()
      : mode === "pipeline" ? pipePaneHtml()
      : mode === "cfg" ? cfgPaneHtml()
        : mode === "blame" ? blamePaneHtml()
          : mode === "diff" ? diffPaneHtml()
            : mode === "src" ? srcPaneHtml()
              : mode === "analyses" ? analysesPaneHtml()
                : mode === "isel" ? iselPaneHtml()
                  : irPaneHtml();
  if (mode === "src") applySrcHighlight("cmapside");
  if (mode === "isel") applyIselHighlight();
  applyBlameHighlight();
  const first = split.querySelector(".irpair > .irside");
  if (first) first.style.flex = `0 0 ${(STATE.splitRatio * 100).toFixed(1)}%`;
  mountCfgGraphs();
  mountPipeGraphs();
  updateViewHead();  // after the panes: it reads what they rendered
}

/* --- ISel view: LLVM IR <-> machine IR ------------------------------------- */

function iselPaneHtml() {
  const empty = (note) => pane("ISEL", "", "", `<div class="cfg-empty">${note}</div>`);
  const corr = ((CURRENT_PASS || {}).iselMap || {})[STATE.fn];
  const change = fnChange(STATE.fn);
  if (!corr || !change) {
    return empty("(no IR/machine correlation here — this view belongs to instruction "
      + "selection, the pass where machine IR is born)");
  }
  if (STATE.iselBlock != null && STATE.iselBlock >= corr.irBlocks.length) {
    STATE.iselBlock = null;  // a stale pick from the function shown before
    STATE.iselRefs = [];
  }

  const irLines = splitLines(corr.ir);
  const mirLines = splitLines(change.after);
  const irBlockOf = new Array(irLines.length).fill(-1);
  corr.irBlocks.forEach((b, i) => {
    for (let line = b.start; line <= b.end && line < irLines.length; line++) irBlockOf[line] = i;
  });
  const mirPairOf = new Array(mirLines.length).fill(-1);
  const inMirBlock = new Array(mirLines.length).fill(false);
  corr.mirBlocks.forEach(b => {
    for (let line = b.start; line <= b.end && line < mirLines.length; line++) {
      inMirBlock[line] = true;
      if (b.irBlock != null) mirPairOf[line] = b.irBlock;
    }
  });
  const selected = new Set(corr.mirBlocks.map(b => b.irBlock).filter(i => i != null));

  const row = (text, cls, attrs) =>
    `<div class="urow${cls}"${attrs}><span class="uln">${text[1]}</span>`
    + `<code class="utext">${highlightIR(text[0]) || "&nbsp;"}</code></div>`;

  const irRows = irLines.map((text, i) => {
    const pair = irBlockOf[i];
    // An IR block no machine block claims did not survive selection.
    const cls = pair < 0 ? "" : selected.has(pair) ? " ilink" : " ilink idrop";
    return row([text, i + 1], cls, pair < 0 ? ` data-irln="${i}"` : ` data-blk="${pair}" data-irln="${i}"`);
  }).join("");

  const mirRows = mirLines.map((text, i) => {
    const pair = mirPairOf[i];
    // A machine block with no IR name is the backend's own invention.
    const cls = pair >= 0 ? " ilink" : inMirBlock[i] ? " inew" : "";
    const refs = corr.refs[String(i)];
    return row([text, i + 1], cls,
      (pair >= 0 ? ` data-blk="${pair}"` : "") + (refs ? ` data-refs="${refs.join(",")}"` : ""));
  }).join("");

  const traced = corr.mirBlocks.filter(b => b.irBlock != null).length;
  const dropped = corr.irBlocks.length - selected.size;
  const stat = `${traced}/${corr.mirBlocks.length} machine blocks traced`
    + ` · ${Object.keys(corr.refs).length} value refs`
    + (dropped ? ` · ${dropped} IR block${dropped > 1 ? "s" : ""} not selected` : "");
  const side = (label, body, cls) => `
    <div class="irside ${cls}">
      <div class="irside-head"><span>${label}</span></div>
      <div class="udiff"><div class="ubody">${body}</div></div>
    </div>`;
  const body = `
    <div class="irpair${STATE.orientation === "stack" ? " stacked" : ""}">
      ${side("llvm ir · entering isel", irRows, "iselir")}
      <div class="divider" title="drag to resize"></div>
      ${side("machine ir · after isel", mirRows, "iselmir")}
    </div>`;
  return pane("ISEL", "", stat, body);
}

function applyIselHighlight(scrollTo) {
  const pair = STATE.iselBlock == null ? null : String(STATE.iselBlock);
  document.querySelectorAll("#split .urow[data-blk]").forEach(r =>
    r.classList.toggle("hit", pair !== null && r.dataset.blk === pair));
  const refs = new Set(STATE.iselRefs || []);
  document.querySelectorAll("#split .urow[data-irln]").forEach(r =>
    r.classList.toggle("vref", refs.has(+r.dataset.irln)));
  if (!scrollTo) return;
  const target = document.querySelector(`#split .${scrollTo} .urow.vref`)
    || document.querySelector(`#split .${scrollTo} .urow.hit`);
  if (target) scrollRowIntoView(target);
}

/* --- bottom panel ---------------------------------------------------------- */

// [{ tab, count }] -- the count is what the tab holds, shown before you open it.
function bottomTabs() {
  if (isInputCard()) return [{ tab: "Log" }];
  const runs = ((currentPassSummary() || {}).analysisCounts || {}).run;
  const tabs = [{ tab: "Log" }, { tab: "Analyses", count: runs || 0 }];
  const regMap = CURRENT_PASS && (CURRENT_PASS.regMap || {})[STATE.fn];
  if (regMap) tabs.push({ tab: "RegMap", count: Object.keys(regMap).length });
  const sites = CURRENT_PASS && ((CURRENT_PASS.spillSites || {})[STATE.fn] || []);
  if (sites && sites.length) tabs.push({ tab: "Spills", count: sites.length });
  if (CURRENT_PASS && CURRENT_PASS.lane === "mir" && CURRENT_PASS.asm) tabs.push({ tab: "Asm" });
  return tabs;
}

function renderBottom() {
  const tabs = bottomTabs();
  // Which tabs exist varies per pass, so a tab that is missing here is drawn
  // over rather than forgotten: the choice has to survive the passes that
  // cannot offer it (the input card offers almost none).
  const wanted = STATE.bottomTab;
  if (!tabs.some(t => t.tab === wanted)) STATE.bottomTab = "Log";
  document.getElementById("bottomTabs").innerHTML = tabs.map(t =>
    `<button class="vtab ${t.tab === STATE.bottomTab ? "active" : ""}" data-tab="${t.tab}">` +
    `${t.tab}${t.count ? `<span class="count">${t.count}</span>` : ""}</button>`).join("");
  document.getElementById("bottom").classList.toggle("open", STATE.bottomOpen);
  document.getElementById("bottomBody").innerHTML = STATE.bottomOpen ? bottomBodyHtml() : "";
  renderStrip(tabs);
  STATE.bottomTab = wanted;
  document.querySelectorAll("#bottomTabs .vtab").forEach(b =>
    b.addEventListener("click", () => { STATE.bottomTab = b.dataset.tab; renderBottom(); }));
}

function renderStrip(tabs) {
  const s = currentPassSummary();
  const muted = text => `<span class="muted">${text}</span>`;
  document.getElementById("stripWho").innerHTML = s
    ? `#${String(s.runIndex).padStart(3, "0")} ${escapeHtml(s.name)}`
      + (s.timeMs != null ? " " + muted(`· ${s.timeMs.toFixed(2)} ms`) : "")
      + (STATE.fn ? " " + muted(`· ${escapeHtml(STATE.fn)}`) : "")
    : "no pass selected";
  document.getElementById("stripBadges").innerHTML = tabs.map(t => {
    const on = STATE.bottomOpen && t.tab === STATE.bottomTab;
    const cls = ["strip-badge", on ? "on" : "", t.tab === "Spills" ? "warn" : ""];
    return `<span class="${cls.filter(Boolean).join(" ")}" data-tab="${t.tab}">` +
      `${t.count ? t.count + " " : ""}${t.tab.toLowerCase()}</span>`;
  }).join("");
  document.getElementById("strip").setAttribute("aria-expanded", String(STATE.bottomOpen));
  document.getElementById("stripCaret").textContent = STATE.bottomOpen ? "⌄" : "⌃";
}

function bottomBodyHtml() {
  const d = CURRENT_PASS;
  if (!d) return '<p class="cfg-empty">(select a pass)</p>';
  if (STATE.bottomTab === "Log") {
    return d.log ? `<pre class="raw">${escapeHtml(d.log)}</pre>` : "";
  }
  if (STATE.bottomTab === "Analyses") {
    const a = d.analyses || {};
    return ANALYSIS_BUCKETS
      .filter(bucket => (a[bucket] || []).length)
      .map(bucket => `
        <h3>${bucket} (${a[bucket].length})</h3>
        <pre class="raw">${escapeHtml(a[bucket].join("\n"))}</pre>`)
      .join("");
  }
  if (STATE.bottomTab === "RegMap") {
    const map = (d.regMap || {})[STATE.fn];
    if (!map) return "";
    return `<h3>${escapeHtml(STATE.fn)} (${Object.keys(map).length})</h3>
      <table class="grid"><tr><th>vreg</th><th>physreg / slot</th></tr>`
      + Object.entries(map)
        .map(([v, p]) => `<tr><td>%${escapeHtml(v)}</td><td>${escapeHtml(p)}</td></tr>`)
        .join("") + `</table>`;
  }
  if (STATE.bottomTab === "Spills") {
    const sites = (d.spillSites || {})[STATE.fn] || [];
    if (!sites.length) return "";
    const stores = sites.filter(s => s.kind === "spill").length;
    return `<h3>${escapeHtml(STATE.fn)} (${stores} spill, ${sites.length - stores} reload)</h3>
      <div class="tscroll"><table class="grid">
        <tr><th>kind</th><th>slot</th><th>block</th><th>instruction</th></tr>`
      + sites.map(s => `<tr><td class="${escapeHtml(s.kind)}">${escapeHtml(s.kind)}</td>`
        + `<td>%stack.${escapeHtml(s.slot)}</td><td>${escapeHtml(s.block)}</td>`
        + `<td class="instr">${escapeHtml(s.text)}</td></tr>`).join("")
      + `</table></div>`;
  }
  if (STATE.bottomTab === "Asm") return `<pre class="raw">${escapeHtml(d.asm)}</pre>`;
  return "";
}

/* --- CFG graph rendering (cytoscape + dagre, vendored in vendor/) --------- */

let CFG_PENDING = [];
const CFG_INSTANCES = new Set();

const CFG_COLORS = {
  node: "#1b1f24", border: "#333a43", entry: "#74c48a",
  text: "#dfe4ea", edge: "#4a535f", back: "#e3767f", accent: "#7aa2f7",
};

const CFG_FONT = { size: 10, charW: 6.0, lineH: 14, padX: 10, padY: 8, maxW: 300 };

// How many characters one label line holds inside a box of this width.
function labelChars(maxW, charW) {
  return Math.max(1, Math.floor(((Number(maxW) || 0) - 4) / (Number(charW) || 1)));
}

function labelBox(label, charW = CFG_FONT.charW, maxW = CFG_FONT.maxW) {
  const { lineH, padX, padY } = CFG_FONT;
  const maxChars = labelChars(maxW, charW);
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
  PIPE_PENDING = [];
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
        "text-wrap": "wrap",
        "text-max-width": `${CFG_FONT.maxW}px`,
        "text-valign": "center",
        "text-halign": "center",
        "padding": "0px",
        // Lift labels off the graticule dots behind the canvas.
        "text-outline-color": "#0b0d10",
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
      { selector: "edge.loop", style: {
        "loop-direction": "-45deg",
        "loop-sweep": "-90deg",
        "control-point-step-size": 60,
      }},
    ],
  });
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

/* --- pipeline flow: collapsed pass chains, one serpentine ----------------- */

// Mirrors :root in style.css. Cytoscape paints to a canvas and cannot read
// custom properties, so these literals are the one place tokens are duplicated.
const PIPE_COLORS = {
  node: "#1b1f24",   // --panel2
  agg: "#15181c",    // --panel
  border: "#333a43", // --line-strong
  soft: "#4a535f",   // muted rule
  // A changed step is the content of this view, so it carries the brighter
  // outline; an unchanged one is connective tissue and recedes.
  nodeFg: "#232830",  // changed fill, lifted off the canvas
  nodeBd: "#5a6675",  // changed outline
  ghost: "#6b737e",   // unchanged text
  ghostBd: "#3a424c", // unchanged outline
  ghostFg: "#101317", // unchanged fill once its opacity composites over --well
  ink: "#dfe4ea",    // --ink
  dim: "#8b939e",    // --dim
  faint: "#5d656f",  // --faint
  trace: "#7aa2f7",  // --trace
  entry: "#74c48a",  // --entry
  warn: "#d9a441",   // --warn
  del: "#e3767f",    // --del
  add: "#74c48a",    // --add
  custom: "#b28cf0", // --custom
};

// Two lines per box: what it is, then what it cost. Everything else lives in
// the detail panel, which is what lets the type be readable.
const PIPE_FONT = { size: 12, lineH: 16, padX: 11, padY: 10, maxW: 190, minW: 150 };
const PIPE_CELL_H = 58;    // a pass cell: 2 label lines
const PIPE_AGG_H = 58;     // collapsed-span header
const PIPE_CHILD_H = 42;   // one inlined pass inside an expanded span
const PIPE_COL_GAP = 40;
const PIPE_LEGEND_TRACK = 26;   // how long a key bar is, filled to its share
const PIPE_BAR_H = 3;           // how tall a cell's bar is
const PIPE_BAR_INSET = 7;       // clear of the cell's rounded corner
const PIPE_BAR_LIFT = 3;        // clear of the cell's bottom border
const PIPE_ROW_GAP = 46;
const PIPE_MARGIN = 22;
const PIPE_COLS = 5;          // cells per row, the target
const PIPE_MIN_CELL_W = 150;  // below this a cell is too narrow to hold a label
const PIPE_TAG_H = 20;
const PIPE_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

let PIPE_PENDING = [];
let PIPE_EXPANDED = new Set();   // collapsed-span keys, e.g. "ir:41-52"
let PIPE_SELECTED = null;        // selected node key, kept across re-renders

// -- collapse: one node per change, one node per run of non-changes --

// Nodes carry added/removed flat; only a raw pass nests them under lineDelta.
function pipeChurn(n) {
  return ((n && n.added) || 0) + ((n && n.removed) || 0);
}

// A pass owns its own node when it changed something or is a custom pass.
// isCustom must break a run: collapsing it would hide it from the view while
// every other surface in the report still shows it.
function pipeOwnNode(p) {
  return !!(p.isInput || p.changed || p.isCustom);
}

function pipeSeq(passes, lane) {
  return (passes || [])
    .filter(p => p.lane === lane)
    .sort((a, b) => a.runIndex - b.runIndex);
}

function pipeNodeFrom(p, kind) {
  const a = p.analysisCounts || {};
  return {
    key: "p" + p.id, kind, summaryId: p.id, name: p.name, idx: p.runIndex,
    lane: p.lane, timeMs: p.timeMs, added: (p.lineDelta || {}).added || 0,
    removed: (p.lineDelta || {}).removed || 0,
    spills: p.spillCount || 0, run: a.run || 0, invalidated: a.invalidated || 0,
    custom: !!p.isCustom, isel: !!(p.iselFns && p.iselFns.length),
    entry: !!p.isInput, changed: !!p.changed, ids: [p.id], count: 1,
  };
}

function pipeCollapse(passes, lane, onlyChanged) {
  const seq = pipeSeq(passes, lane);
  const nodes = [];
  let i = 0;
  while (i < seq.length) {
    if (pipeOwnNode(seq[i])) {
      nodes.push(pipeNodeFrom(seq[i], "pass"));
      i++;
      continue;
    }
    let j = i;
    while (j < seq.length && !pipeOwnNode(seq[j])) j++;
    const run = seq.slice(i, j);
    i = j;

    if (run.length === 1) {
      // A one-pass "span" would be a dead interaction — show it under its own
      // name, dimmed, instead.
      if (!onlyChanged) nodes.push(pipeNodeFrom(run[0], "noop"));
      continue;
    }
    const times = run.map(p => p.timeMs).filter(v => v != null);
    const head = run[0], tail = run[run.length - 1];
    const sum = pick => run.reduce((s, p) => s + pick(p), 0);
    nodes.push({
      key: `a${lane}:${head.runIndex}-${tail.runIndex}`, kind: "agg",
      summaryId: null, name: run.length + " unchanged", idx: head.runIndex,
      lane, from: head.runIndex, to: tail.runIndex, count: run.length,
      timeMs: times.length ? times.reduce((s, v) => s + v, 0) : null,
      timedCount: times.length,
      added: sum(p => (p.lineDelta || {}).added || 0),
      removed: sum(p => (p.lineDelta || {}).removed || 0),
      spills: sum(p => p.spillCount || 0),
      run: sum(p => (p.analysisCounts || {}).run || 0),
      invalidated: sum(p => (p.analysisCounts || {}).invalidated || 0),
      custom: false, isel: false, entry: false, changed: false,
      ids: run.map(p => p.id), children: run,
      // "changed only" already means "spans are collapsed", so there is nothing
      // to expand into there.
      expandable: !onlyChanged,
    });
  }
  return { nodes, stats: pipeStats(seq, nodes) };
}

function pipeStats(seq, nodes) {
  const timed = seq.filter(p => p.timeMs != null);
  const nonzero = timed.filter(p => p.timeMs > 0);
  return {
    passes: seq.length,
    changed: seq.filter(p => p.changed || p.isCustom).length,
    nodes: nodes.length,
    aggregates: nodes.filter(n => n.kind === "agg").length,
    collapsedPasses: nodes.reduce((s, n) => s + (n.kind === "agg" ? n.count : 0), 0),
    laneTotalMs: timed.reduce((s, p) => s + p.timeMs, 0),
    timedFraction: timed.length ? nonzero.length / timed.length : 1,
  };
}

// -- layout: rows that alternate direction, wrapping under the previous tail --

function pipeRowRule(rowLen, prevLastSlot) {
  const startSlot = prevLastSlot == null ? 0 : prevLastSlot;
  const step = (startSlot === 0 || rowLen - 1 > startSlot) ? 1 : -1;
  return { startSlot, step };
}

// The pitch the grid repeats at: a full-width cell plus the gap after it.
function pipeColStride() {
  return PIPE_FONT.maxW + 2 * PIPE_FONT.padX + PIPE_COL_GAP;
}

// Cells are sized to the pane, so the target column count always fits. Pinning
// five per row only works if the cells shrink to make room for the fifth; a
// fixed cell width would push the row off the pane, reachable only by panning.
function pipeGrid(clientW) {
  const avail = Math.max(0, (clientW || 0) - 2 * PIPE_MARGIN);
  const fit = n => Math.floor((avail - (n - 1) * PIPE_COL_GAP) / n);
  let cols = PIPE_COLS;
  let cellW = fit(cols);
  // Too narrow for five readable cells: give up a column rather than shrink on.
  while (cols > 3 && cellW < PIPE_MIN_CELL_W) cellW = fit(--cols);
  cellW = Math.max(PIPE_MIN_CELL_W,
    Math.min(cellW, PIPE_FONT.maxW + 2 * PIPE_FONT.padX));
  return {
    cols, cellW, stride: cellW + PIPE_COL_GAP,
    // what the label may occupy inside the cell
    maxW: cellW - 2 * PIPE_FONT.padX,
    gap: PIPE_COL_GAP, floor: PIPE_MIN_CELL_W, target: PIPE_COLS,
  };
}

// One cell per pipeline step; an expanded span stacks its members in place.
function pipeCells(nodes, expanded) {
  return nodes.map(n => {
    if (n.kind === "agg" && n.expandable && expanded && expanded.has(n.key)) {
      const items = [{ ...n, h: PIPE_AGG_H, head: true, expanded: true }];
      for (const c of n.children) {
        items.push({ ...pipeNodeFrom(c, "child"), h: PIPE_CHILD_H, child: true });
      }
      return { key: n.key, kind: n.kind, items, h: items.reduce((a, x) => a + x.h + 4, 0) };
    }
    return { key: n.key, kind: n.kind, items: [{ ...n, h: PIPE_CELL_H }], h: PIPE_CELL_H };
  });
}

function pipeLayout(cells, opts) {
  const cols = Math.max(1, opts.cols | 0);
  const rows = [];
  for (let i = 0; i < cells.length; i += cols) rows.push(cells.slice(i, i + cols));

  const rowH = rows.map(r => Math.max(1, ...r.map(c => c.h)));
  const rowDirs = [], tags = [], positions = {};
  let y = PIPE_MARGIN, prevLast = null, maxSlot = 0, r = 0;
  for (const row of rows) {
    const { startSlot, step } = pipeRowRule(row.length, prevLast);
    const tagY = y;
    y += PIPE_TAG_H;
    const top = y;
    y += rowH[r] + PIPE_ROW_GAP;

    let last = startSlot;
    row.forEach((cell, c) => {
      const slot = startSlot + c * step;
      last = slot;
      maxSlot = Math.max(maxSlot, slot);
      const cx = PIPE_MARGIN + slot * opts.colStride + opts.colStride / 2;
      let iy = top;
      for (const item of cell.items) {
        positions[item.key] = { x: cx, y: iy + item.h / 2 };
        iy += item.h + 4;
      }
    });

    const tagX = PIPE_MARGIN + startSlot * opts.colStride + opts.colStride / 2;
    const tagKey = "t" + r;
    // Tags live in the same position map so the preset layout covers them too.
    positions[tagKey] = { x: tagX, y: tagY + PIPE_TAG_H / 2 };
    const head = row[0], tail = row[row.length - 1];
    tags.push({
      key: tagKey, row: r, dir: step, x: tagX, y: tagY + PIPE_TAG_H / 2,
      label: `${step > 0 ? "▸" : "◂"} ROW ${r + 1} · ${pipeIdx(head.items[0].idx)}–${pipeIdx(tail.items[0].idx)}`,
    });
    rowDirs.push(step);
    prevLast = last;
    r++;
  }

  const slots = Math.max(cols, maxSlot + 1);
  // Cell indices after which the chain starts a new row; those edges wrap.
  const wrapAfter = [];
  let seen = 0;
  for (let i = 0; i + 1 < rows.length; i++) {
    seen += rows[i].length;
    wrapAfter.push(seen - 1);
  }
  return {
    positions, tags, rowDirs, rows: rows.map(x => x.length), rowH, wrapAfter,
    width: PIPE_MARGIN * 2 + slots * opts.colStride,
    height: Math.max(80, y - PIPE_ROW_GAP + PIPE_MARGIN),
  };
}

// -- labels and elements --

function pipeIdx(i) { return "#" + String(i == null ? 0 : i).padStart(3, "0"); }

// A 0.00 is the 0.1 ms reporting floor, not a measurement, and a word would be
// too long for a cell: the dash stands for both, and the note above says why.
function pipeTime(v) {
  return (v == null || v === 0) ? "—" : v.toFixed(2) + " ms";
}

// Names every mark the graph can draw. Built from the same tokens the styles
// paint with, so the key cannot drift from the picture it describes.
function pipeLegend() {
  const c = PIPE_COLORS;
  const box = (bd, bg, dashed) =>
    `<i class="pl-sw" style="border-color:${bd};background-color:${bg};`
    + (dashed ? "border-style:dashed" : "") + '"></i>';
  const line = (col, dashed, w) =>
    `<i class="pl-ln" style="border-top-color:${col};border-top-width:${w}px;`
    + `border-top-style:${dashed ? "dashed" : "solid"}"></i>`;
  // The track wears the cell fill: in the key's own panel colour it draws nothing.
  const bar = segs => `<i class="pl-bar" style="background-color:${c.nodeFg};`
    + `width:${PIPE_LEGEND_TRACK}px">`
    + segs.map(([col, w]) =>
        `<s style="background-color:${col};width:${w}px"></s>`).join("") + "</i>";
  const it = (mark, text) => `<span class="pl-it">${mark}<em>${text}</em></span>`;
  const code = (s, text) => it(`<b>${s}</b>`, text);
  return '<div class="pipe-legend">'
    + it(box(c.nodeBd, c.nodeFg), "changed")
    + it(box(c.ghostBd, c.nodeFg, 1), "unchanged")
    + it(box(c.ghostBd, c.agg, 1), "collapsed span")
    + it(box(c.ghostBd, c.agg), "span member")
    + it(box(c.entry, c.nodeFg), "entry")
    + it(box(c.custom, c.nodeFg), "custom")
    + it(box(c.trace, c.nodeFg), "isel · selected")
    + it(line(c.soft, 0, 1.3), "next pass")
    + it(line(c.soft, 1, 1.6), "row break")
    + it(line(c.trace, 1, 2.2), "opt → llc")
    + it(bar([[c.del, 8], [c.add, 8]]), "lines removed / added")
    + it(bar([[c.trace, 20]]), "share of time")
    + code("#012", "run ordinal")
    + code("+5 −2", "line churn")
    + code("—", "below 0.1 ms")
    + "</div>";
}

// Everything the box was too small to hold: the full metrics for the selected
// node, and the only two actions the view has, as real buttons.
function pipeDetailHtml(n) {
  if (!n) return "";
  const stats = [];
  if (n.kind === "agg") {
    stats.push([`${n.count} passes`, pipeIdx(n.from) + "–" + pipeIdx(n.to)]);
    stats.push(["time", pipeTime(n.timeMs)]);
    stats.push(["churn", pipeChurn(n) ? `+${n.added} −${n.removed}` : "none"]);
  } else {
    stats.push(["time", pipeTime(n.timeMs)]);
    if (!n.entry) {
      stats.push(["churn", pipeChurn(n) ? `+${n.added} −${n.removed}` : "none"]);
    }
  }
  if (n.spills) stats.push(["spills", String(n.spills)]);
  if (n.run || n.invalidated) stats.push(["analyses", `${n.run} run · ${n.invalidated} invalid`]);
  if (n.custom) stats.push(["custom", "yes"]);
  if (n.isel) stats.push(["isel", "selection pass"]);
  if (n.entry) stats.push(["card", "pipeline entry"]);

  const cells = stats.map(([k, v]) =>
    `<span class="pm"><b>${escapeHtml(k)}</b>${escapeHtml(v)}</span>`).join("");

  const acts = [];
  // Only a span that was collapsed can be expanded; a filter already flattened
  // the rest, and offering it there would be a dead button.
  if (n.kind === "agg" && n.expandable) {
    acts.push(`<button class="pipe-act" data-pipe-act="toggle" data-pipe-key="${n.key}">`
      + `${n.expanded ? "▾ collapse" : "▸ expand"} (${n.count})</button>`);
  }
  if (n.summaryId != null) {
    acts.push(`<button class="pipe-act" data-pipe-act="cfg" data-pipe-key="${n.key}">open CFG</button>`);
    acts.push(`<button class="pipe-act" data-pipe-act="diff" data-pipe-key="${n.key}">open Diff</button>`);
  }

  const head = n.kind === "agg"
    ? `${n.count} passes · unchanged`
    : `${pipeIdx(n.idx)}  ${n.name}`;
  // Members are numbered by runIndex, the same as the range in the header, so the
  // two lines agree. Pass ids are global and would read as a different range.
  const members = n.kind === "agg" && n.children && n.children.length
    ? `<div class="pipe-members">holds `
      + escapeHtml(n.children.map(c => pipeIdx(c.runIndex)).join(" ")) + `</div>`
    : "";
  return `<div class="pipe-detail-in">
      <div class="pipe-detail-head">${escapeHtml(head)}</div>
      <div class="pipe-detail-stats">${cells}</div>
      ${members}
      <div class="pipe-detail-acts">${acts.join("")}</div>
    </div>`;
}

function pipeBadges(n) {
  const b = [];
  if (n.custom) b.push("custom");
  if (n.isel) b.push("ISEL");
  if (n.spills) b.push("⚠" + n.spills);
  if (n.run || n.invalidated) b.push(`+${n.run} −${n.invalidated}`);
  if (n.added || n.removed) b.push(`+${n.added} −${n.removed}`);
  return b.join(" · ");
}

// A name that outruns the cell is clipped, never wrapped: a head spilling onto
// line 2 would push the time line off the box and read as a pass with no time.
function clipLine(s, maxChars) {
  const t = String(s);
  return t.length <= maxChars ? t : t.slice(0, Math.max(1, maxChars - 1)) + "…";
}

// Two lines only, the reading first, so it starts in the same place on every
// box; churn or a one-word verdict follows it.
function pipeNodeLabel(n, charW, maxLabelW) {
  const head = n.kind === "agg"
    ? `${pipeIdx(n.from)}–${pipeIdx(n.to)}  ${n.count} passes`
    : `${pipeIdx(n.idx)}  ${n.name}`;
  let tail;
  if (n.kind === "agg") tail = "unchanged";
  else if (n.entry) tail = "entry";
  else if (n.kind === "noop") tail = "unchanged";
  else tail = pipeChurn(n) > 0 ? `+${n.added} −${n.removed}` : "";
  // Capped from the caller's own box width, so the label cannot wrap past the
  // two lines a cell holds however narrow the pane gets.
  const cap = labelChars(maxLabelW, charW);
  const line2 = [pipeTime(n.timeMs), tail].filter(Boolean).join("  ");
  // The time leads, so clipping a narrow box costs the churn, never the time.
  return [clipLine(head, cap), clipLine(line2, cap)].join("\n");
}

function pipeBar(n, kind, ctx) {
  const w = 120, h = PIPE_BAR_H;
  // A span's bar is its share of its own lane, not of every lane drawn.
  const laneMs = (ctx.laneTotals && ctx.laneTotals[n.lane]) || ctx.laneTotalMs;
  let segs = [];
  if (kind === "pass" && ctx.maxChurn > 0 && pipeChurn(n) > 0) {
    const scale = w / ctx.maxChurn;
    segs = [
      { w: Math.max(1, Math.round(n.removed * scale)), c: PIPE_COLORS.del },
      { w: Math.max(1, Math.round(n.added * scale)), c: PIPE_COLORS.add },
    ];
  } else if (kind === "agg" && n.timeMs && laneMs > 0) {
    segs = [{ w: Math.max(2, Math.round(n.timeMs / laneMs * w)), c: PIPE_COLORS.trace }];
  }
  const used = segs.reduce((s, x) => s + x.w, 0);
  // Set in from both ends, so either pinning edge clears that corner's curve.
  const rects = segs.map((s, i) => {
    const x = PIPE_BAR_INSET + segs.slice(0, i).reduce((a, b) => a + b.w, 0);
    return `<rect x="${x}" y="0" width="${s.w}" height="${h}" fill="${s.c}"/>`;
  }).join("");
  const width = Math.max(used, 1) + 2 * PIPE_BAR_INSET;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" `
    + `height="${h + PIPE_BAR_LIFT}">${rects}</svg>`;
  return { bg: "data:image/svg+xml;utf8," + encodeURIComponent(svg), bw: used ? width + "px" : "0px" };
}

function pipeClassOf(n) {
  const cls = [];
  if (n.kind === "agg") cls.push("agg");
  else if (n.kind === "noop") cls.push("noop");
  else if (n.kind === "child") cls.push("childins");
  else cls.push("pass");
  if (n.entry) cls.push("entry");
  if (n.custom) cls.push("custom");
  if (n.isel) cls.push("isel");
  return cls.join(" ");
}

function pipeGraphSpec(manifest, opts) {
  const passes = (manifest && manifest.passes) || [];
  const lanes = opts.lane === "both" ? ["ir", "mir"] : [opts.lane];
  const expanded = opts.expanded || new Set();
  // The pane decides how wide a cell may be; the default is the widest allowed.
  const maxLabelW = opts.maxW || PIPE_FONT.maxW;

  const parts = {};
  for (const l of lanes) parts[l] = pipeCollapse(passes, l, opts.onlyChanged);
  const all = [];
  for (const l of lanes) all.push(...parts[l].nodes);

  const laneTotals = {};
  for (const l of lanes) laneTotals[l] = parts[l].stats.laneTotalMs;
  const laneTotalMs = lanes.reduce((s, l) => s + laneTotals[l], 0);
  const maxChurn = Math.max(1, ...all.map(n => pipeChurn(n)));
  const ctx = { laneTotalMs, laneTotals, maxChurn };

  const cells = pipeCells(all, expanded);
  const legend = pipeLayout(cells, opts);

  // Element data, keyed the same way as the layout.
  // `order` is the cell chain (what the edges follow); `itemKeys` is every
  // element to mount. They differ once a span is expanded, whose children are
  // mounted even though the chain still steps cell to cell.
  const data = {}, classes = {}, order = [], itemKeys = [], raw = {};
  cells.forEach(cell => {
    for (const item of cell.items) {
      const box = labelBox(pipeNodeLabel(item, opts.charW, maxLabelW),
                           opts.charW, maxLabelW);
      const bar = pipeBar(item, cell.kind, ctx);
      // `raw` keeps the whole node for the detail panel; only `data` is lean
      // enough to hand cytoscape.
      raw[item.key] = item;
      data[item.key] = {
        id: item.key,
        label: box.wrapped,
        w: clampW(box.w, maxLabelW), h: item.h,
        bg: bar.bg, bw: bar.bw,
        kind: item.kind, summaryId: item.summaryId, aggKey: cell.key,
        expandable: !!item.expandable, spills: item.spills,
        custom: item.custom, lane: item.lane,
      };
      classes[item.key] = pipeClassOf(item);
      itemKeys.push(item.key);
    }
    order.push(cell.key);
  });

  // The chain: one edge between consecutive steps. Where it crosses lanes it
  // becomes the handoff, and where it crosses a row it is a wrap.
  const wraps = new Set(legend.wrapAfter || []);
  const edges = [];
  for (let i = 0; i + 1 < cells.length; i++) {
    const a = cells[i].items[cells[i].items.length - 1];
    const b = cells[i + 1].items[0];
    edges.push({
      id: `e${a.key}-${b.key}`, source: a.key, target: b.key,
      cls: a.lane !== b.lane ? "handoff" : (wraps.has(i) ? "wrap" : ""),
    });
  }

  const stats = {
    lanes,
    passes: lanes.reduce((s, l) => s + parts[l].stats.passes, 0),
    changed: lanes.reduce((s, l) => s + parts[l].stats.changed, 0),
    nodes: all.length,
    aggregates: lanes.reduce((s, l) => s + parts[l].stats.aggregates, 0),
    timedFraction: Math.min(...lanes.map(l => parts[l].stats.timedFraction)),
  };
  return {
    data, raw, classes, edges, order, itemKeys, tags: legend.tags,
    positions: legend.positions,
    width: legend.width, height: legend.height, rows: legend.rows,
    wrapAfter: legend.wrapAfter, stats, laneTotalMs,
  };
}


// The floor keeps a short label from collapsing to a sliver, but an explicit
// ceiling always wins: a caller that says 120 must not get 150 back.
function clampW(w, maxLabelW) {
  const ceiling = (maxLabelW || PIPE_FONT.maxW) + 2 * PIPE_FONT.padX;
  return Math.min(ceiling, Math.max(PIPE_FONT.minW, w));
}

// -- pane, mount, interaction --

function pipeBodyHtml(idx, label) {
  return `
    <div class="pipe-cy" data-idx="${idx}">
      <div class="pipe-cy-frame">
        <span class="flabel">${escapeHtml(label)}</span>
        <i class="cb tl" aria-hidden="true"></i><i class="cb tr" aria-hidden="true"></i>
        <i class="cb bl" aria-hidden="true"></i><i class="cb br" aria-hidden="true"></i>
        <div class="pipe-cy-canvas"></div>
        <div class="pipe-cy-zoom">ZOOM ×1.00</div>
      </div>
      <div class="pipe-cy-detail"></div>
    </div>`;
}

function pipePaneHtml() {
  const manifest = CURRENT_MANIFEST || { passes: [] };
  const passes = manifest.passes || [];
  if (!passes.length) {
    return pane("FLOW", "", "", '<div class="cfg-empty">(no passes captured)</div>');
  }
  const onlyChanged = document.getElementById("changedOnly").checked;
  const lane = STATE.pipeBoth ? "both" : STATE.lane;

  const shown = lane === "both" ? ["ir", "mir"] : [lane];
  const stats = shown.map(l => pipeCollapse(passes, l, onlyChanged).stats);
  const total = stats.reduce((s, x) => s + x.nodes, 0);
  const changed = stats.reduce((s, x) => s + x.changed, 0);

  const chips = `<span class="splt">
      <button class="ptab${STATE.pipeBoth ? " active" : ""}" data-pipe="both">both</button>
      <button class="ptab${STATE.pipeBoth ? "" : " active"}" data-pipe="lane">${STATE.lane === "ir" ? "IR" : "machine"}</button>
    </span>`;

  const stat = `${total} nodes · ${changed} changed`;

  const idx = PIPE_PENDING.length;
  PIPE_PENDING.push({ lane, onlyChanged });
  const label = lane === "both" ? "pipeline · opt → llc" : `pipeline · ${lane === "ir" ? "opt" : "llc"}`;
  return pane("FLOW", chips, stat,
    `<div class="pipe-wrap">${pipeLegend()}${pipeBodyHtml(idx, label)}</div>`);
}

function mountPipeGraphs() {
  document.querySelectorAll("#split .pipe-cy").forEach(el => {
    mountPipe(el, PIPE_PENDING[+el.dataset.idx]);
  });
}

function destroyPipeGraphs() {
  PIPE_PENDING = [];
}

function mountPipe(el, desc) {
  if (!desc) return;
  const canvas = el.querySelector(".pipe-cy-canvas");
  const detail = el.querySelector(".pipe-cy-detail");
  if (!canvas) return;
  if (typeof cytoscape !== "function") {
    el.innerHTML = '<p class="cfg-empty">(graph library failed to load)</p>';
    return;
  }

  const meas = document.createElement("canvas").getContext("2d");
  meas.font = `${PIPE_FONT.size}px ${PIPE_MONO}`;
  const charW = Math.max(meas.measureText("M").width, 1);
  // The grid follows the pane width, so collapsing the rail re-wraps.
  const gridNow = () => pipeGrid(canvas.clientWidth);
  const specFor = g => pipeGraphSpec(CURRENT_MANIFEST, {
    lane: desc.lane, onlyChanged: desc.onlyChanged, cols: g.cols, charW,
    colStride: g.stride, maxW: g.maxW, expanded: PIPE_EXPANDED,
  });
  const elementsFor = spec => {
    const out = [];
    // Every mounted element, not just the cell chain: an expanded span's
    // children are edge endpoints too, and cytoscape rejects an edge whose
    // endpoint was never added.
    for (const key of spec.itemKeys || spec.order) {
      const d = spec.data[key];
      if (!d) continue;
      const cls = spec.classes[key] + (dimmed(key, spec) ? " dim" : "")
        + (key === PIPE_SELECTED ? " sel" : "");
      out.push({ data: d, classes: cls });
    }
    for (const e of spec.edges) {
      out.push({ data: { id: e.id, source: e.source, target: e.target }, classes: e.cls });
    }
    for (const t of spec.tags) {
      out.push({ data: { id: t.key, label: t.label }, position: { x: t.x, y: t.y },
        classes: "rowtag", selectable: false, locked: true });
    }
    return out;
  };

  const grid = gridNow();
  const spec = specFor(grid);
  if (!spec.order.length) {
    el.innerHTML = '<p class="cfg-empty">(no passes match the current filter)</p>';
    return;
  }

  const elements = elementsFor(spec);

  // Same contract as the CFG graph: wheel zooms, dragging blank space pans.
  const cy = cytoscape({
    container: canvas,
    elements,
    minZoom: 0.1, maxZoom: 4,
    boxSelectionEnabled: false,
    autoungrabify: true,   // nodes stay where the layout put them
    style: pipeStyle(),
  });
  // preset takes an id -> {x, y} map; the callback form receives a node, not a key.
  const preset = s => cy.layout(
    { name: "preset", fit: false, padding: 0, positions: s.positions }).run();

  let mounted = { cellW: grid.cellW, spec };

  // Open on the head of the pipeline at 1:1. Fitting the whole graph would
  // shrink 12px labels past reading; zooming out to see the shape is the user's
  // move to make.
  const home = () => {
    const bb = cy.elements().boundingBox();
    const x = bb.w < cy.width() ? (cy.width() - bb.w) / 2 - bb.x1
      : PIPE_MARGIN - bb.x1;
    cy.zoom(1);
    cy.pan({ x, y: PIPE_MARGIN - bb.y1 });
  };
  preset(spec);
  home();

  // Selecting is the view's only click. The two actions it offers are buttons
  // in the detail panel, so no box behaves differently from any other.
  const applySel = key => {
    cy.nodes(".sel").removeClass("sel");
    PIPE_SELECTED = null;
    const item = key == null ? null : mounted.spec.raw[key];
    const n = key == null ? null : cy.$id(key);
    if (item && n && n.nonempty()) {
      n.addClass("sel");
      PIPE_SELECTED = key;
    }
    detail.innerHTML = item ? pipeDetailHtml(item) : "";
  };

  const relayout = () => {
    cy.resize();  // the canvas is CSS-sized, so a resize needs nothing else
    const g = gridNow();
    if (g.cellW === mounted.cellW) return;
    const next = specFor(g);
    const z = cy.zoom(), pan = cy.pan();
    mounted = { cellW: g.cellW, spec: next };
    cy.json({ elements: elementsFor(next) });
    preset(next);
    cy.zoom(z);
    cy.pan(pan);
    applySel(PIPE_SELECTED);
  };

  cy.on("tap", "node", evt => {
    if (evt.target.hasClass("rowtag")) return;
    applySel(evt.target.id());
  });
  cy.on("tap", evt => { if (evt.target === cy) applySel(null); });

  detail.addEventListener("click", evt => {
    const b = evt.target.closest("[data-pipe-act]");
    if (!b) return;
    const item = mounted.spec.raw[b.dataset.pipeKey];
    const act = b.dataset.pipeAct;
    if (act === "toggle") { toggleAgg(item); return; }
    if (!item || item.summaryId == null) return;
    STATE.lastMode = act;
    selectPassFromOverview(item.summaryId);
  });

  const zoomEl = el.querySelector(".pipe-cy-zoom");
  if (zoomEl) {
    const showZoom = () => { zoomEl.textContent = "ZOOM ×" + cy.zoom().toFixed(2); };
    cy.on("zoom", showZoom);
    showZoom();
  }

  applySel(PIPE_SELECTED);

  cy._pipe = { get spec() { return mounted.spec; }, relayout };
  CFG_INSTANCES.add(cy);
  el._cy = cy;
}

// On a both-lane graph the rail's lane tab is a lens, not a filter.
function dimmed(key, spec) {
  const d = spec.data[key];
  return !!(d && spec.stats.lanes.length > 1 && d.lane !== STATE.lane);
}

function toggleAgg(n) {
  if (!n) return;
  const key = n.key;
  if (PIPE_EXPANDED.has(key)) PIPE_EXPANDED.delete(key);
  else PIPE_EXPANDED.add(key);
  renderMain();
}

function pipeStyle() {
  const c = PIPE_COLORS;
  return [
    { selector: "node", style: {
      "background-color": c.nodeFg,
      "background-image": "data(bg)",
      "background-fit": "none",
      "background-width": "data(bw)",
      "background-height": `${PIPE_BAR_H + PIPE_BAR_LIFT}px`,
      "background-position-x": "0px",
      "background-position-y": "100%",
      "background-clip": "none",
      "border-color": c.nodeBd,
      "border-width": 1.5,
      "shape": "round-rectangle",
      "width": "data(w)",
      "height": "data(h)",
      "label": "data(label)",
      "color": c.ink,
      "font-family": PIPE_MONO,
      "font-size": PIPE_FONT.size,
      "text-wrap": "wrap",
      "text-max-width": `${PIPE_FONT.maxW}px`,
      "text-valign": "center",
      "text-halign": "center",
      "padding": "0px",
      "text-outline-color": c.nodeFg,
      "text-outline-width": 2,
      "text-outline-opacity": 0.9,
    }},
    // A span measures time so its bar hangs right; a pass measures churn so its bar stays left.
    { selector: "node.agg", style: {
      "background-color": c.agg, "border-color": c.ghostBd,
      "border-style": "dashed", "border-width": 1.5, "color": c.ghost,
      "text-outline-color": c.agg, "background-position-x": "100%",
    }},
    { selector: "node.noop", style: {
      "border-color": c.ghostBd, "border-style": "dashed", "color": c.ghost,
      "background-opacity": 0.22, "text-outline-color": c.ghostFg,
    }},
    { selector: "node.childins", style: {
      "background-color": c.agg, "border-color": c.ghostBd, "color": c.ghost,
      "border-width": 1, "text-outline-color": c.agg,
      "background-position-x": "100%",
    }},
    { selector: "node.entry", style: { "border-color": c.entry, "border-width": 2 } },
    { selector: "node.custom", style: { "border-color": c.custom, "color": c.custom } },
    { selector: "node.isel", style: { "border-color": c.trace, "border-width": 2 } },
    { selector: "node.sel", style: { "border-color": c.trace, "border-width": 2 } },
    { selector: "node.dim", style: { "opacity": 0.45 } },
    { selector: "node.rowtag", style: {
      "background-opacity": 0, "border-width": 0, "width": 1, "height": 1,
      "label": "data(label)", "font-family": PIPE_MONO, "font-size": 9,
      "color": c.faint, "text-halign": "left", "text-valign": "center",
      "text-wrap": "none", "events": "no", "text-outline-width": 0,
    }},
    { selector: "edge", style: {
      "width": 1.3, "line-color": c.soft, "target-arrow-color": c.soft,
      "target-arrow-shape": "triangle", "arrow-scale": 0.9,
      "curve-style": "bezier",
    }},
    { selector: "edge.handoff", style: {
      "line-color": c.trace, "target-arrow-color": c.trace,
      "width": 2.2, "line-style": "dashed", "opacity": 0.9,
      "label": "opt → llc", "font-family": PIPE_MONO, "font-size": 9,
      "color": c.trace, "text-background-color": "#0b0d10",
      "text-background-opacity": 1, "text-background-padding": "2px",
      "text-wrap": "none",
    }},
    // A wrap is the row's exit column hopping to the next row under it: a
    // layout artifact, not a pipeline event, so it takes the ordinary edge
    // colour and the trace blue is left to mean the lane handoff alone.
    { selector: "edge.wrap", style: {
      "curve-style": "taxi", "taxi-direction": "vertical",
      "line-color": c.soft, "target-arrow-color": c.soft,
      "line-style": "dashed", "width": 1.6, "opacity": 0.8,
    }},
    { selector: "edge.dim", style: { "opacity": 0.3 } },
  ];
}

/* --- boot ---------------------------------------------------------------- */

/* --- command sheet --------------------------------------------------------- */

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
  for (const cy of CFG_INSTANCES) {
    cy.resize();
    if (cy._pipe) cy._pipe.relayout();  // the column count may have changed
  }
}

/* The builder's `ui:` settings: what the report opens on. Every value is
   checked against the same lists the controls use, so a report that was handed
   a stale or hand-edited manifest still opens on something valid. */

// manifest ui key -> the STATE field it fills, and what that field accepts.
const UI_VALUES = {
  lane: ["lane", ["ir", "mir"]],
  mode: ["mode",
    ["cfg", "diff", "ir", "blame", "src", "isel", "analyses", "structure", "pipeline"]],
  orientation: ["orientation", ["side", "stack"]],
};

function applyUiSettings(manifest) {
  const ui = (manifest.metadata || {}).ui;
  if (!ui) return;
  for (const [key, [field, allowed]] of Object.entries(UI_VALUES)) {
    if (allowed.includes(ui[key])) STATE[field] = ui[key];
  }
  // A list, so it is not a membership test like the rest; ANALYSIS_TYPES is the
  // one source of truth, so an unknown name is dropped rather than trusted.
  if (ui.analysis !== undefined) {
    const picked = analysisSelection(ui.analysis);
    STATE.analysisTypes = picked.length ? picked : ["pdt"];
  }
  if (typeof ui.splitRatio === "number" && Number.isFinite(ui.splitRatio)) {
    STATE.splitRatio = Math.min(0.85, Math.max(0.15, ui.splitRatio));
  }
  if (typeof ui.drawer === "boolean") STATE.bottomOpen = ui.drawer;
  if (typeof ui.drawerTab === "string") STATE.bottomTab = ui.drawerTab;
  if (typeof ui.flowBothLanes === "boolean") STATE.pipeBoth = ui.flowBothLanes;
  if (typeof ui.changedOnly === "boolean") {
    document.getElementById("changedOnly").checked = ui.changedOnly;
  }
  // An overview is whole-report; drilling back out of it lands on a detail view.
  STATE.lastMode = OVERVIEW_MODES.includes(STATE.mode) ? "diff" : STATE.mode;
}

async function boot() {
  const manifest = await manifestPromise;
  CURRENT_MANIFEST = manifest;
  applyUiSettings(manifest);
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
    STATE.blameLine = null;
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
  if (!OVERVIEW_MODES.includes(b.dataset.mode)) STATE.lastMode = b.dataset.mode;
  if (STATE.mode !== b.dataset.mode) STATE.blameLine = null;  // lines mean different things per view
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

document.getElementById("split").addEventListener("click", evt => {
  const src = evt.target.closest(".ptab[data-src]");
  if (src) { STATE.cfgSource = src.dataset.src; renderMain(); return; }
  const ctx = evt.target.closest(".ptab[data-ctx]");
  if (ctx) { STATE.diffContext = ctx.dataset.ctx; renderMain(); return; }
  const file = evt.target.closest(".ptab[data-srcfile]");
  if (file) { STATE.srcFile = file.dataset.srcfile; renderMain(); return; }
  const analysis = evt.target.closest(".ptab[data-analysis]");
  if (analysis) {
    const t = analysis.dataset.analysis;
    // A click selects that graph alone; only the file opens on several.
    if (STATE.analysisTypes.length === 1 && STATE.analysisTypes[0] === t) return;  // already alone
    STATE.analysisTypes = [t];
    renderMain();
    return;
  }

  // Flow: [both] [lane] chooses whether the other lane is dimmed or dropped.
  const pipe = evt.target.closest(".ptab[data-pipe]");
  if (pipe) { STATE.pipeBoth = pipe.dataset.pipe === "both"; renderMain(); return; }

  // Structure tree: a group header toggles collapse; a leaf drills into its pass.
  const overview = STATE.mode === "structure" || STATE.mode === "pipeline";
  const head = evt.target.closest(".ptree-head");
  if (head && overview) {
    const group = head.closest(".ptree-group");
    group.classList.toggle("collapsed");
    return;
  }
  const leaf = evt.target.closest(".ptree-leaf[data-id]");
  if (leaf && overview) {
    selectPassFromOverview(+leaf.dataset.id);
    return;
  }

  const link = evt.target.closest("#split .urow[data-blk], #split .urow[data-refs]");
  if (link && STATE.mode === "isel") {
    const pair = link.dataset.blk == null ? null : +link.dataset.blk;
    const refs = link.dataset.refs ? link.dataset.refs.split(",").map(Number) : [];
    const repeat = STATE.iselBlock === pair
      && String(STATE.iselRefs) === String(refs);
    STATE.iselBlock = repeat ? null : pair;
    STATE.iselRefs = repeat ? [] : refs;
    // Scroll the *other* pane: the side you clicked is already in view.
    applyIselHighlight(link.closest(".iselmir") ? "iselir" : "iselmir");
    return;
  }

  // Blame: a click in the chain jumps to the pass that wrote the line.
  const goto = evt.target.closest("#split .brow[data-goto]");
  if (goto) {
    // The pass is the point of the jump, so land on the function it wrote to.
    const fn = STATE.fn;
    selectPass(+goto.dataset.goto).then(() => {
      if (!fn || STATE.fn === fn || !fnNames().includes(fn)) return;
      STATE.fn = fn;
      renderFnList();
      renderMain();
    });
    return;
  }
  if (evt.target.closest("#split .bclose")) {
    STATE.blameLine = null;
    renderMain();
    return;
  }

  // Any IR row can be attributed: blame's own rows, or either side of a diff.
  const bline = evt.target.closest("#split .urow[data-blame]");
  if (bline) {
    const n = +bline.dataset.blame;
    STATE.blameLine = STATE.blameLine === n ? null : n;
    STATE.blameSide = "+";
    renderMain();
    return;
  }
  const dline = evt.target.closest("#split .urow[data-side]");
  if (dline && (STATE.mode === "diff" || STATE.mode === "ir")) {
    const side = dline.dataset.side === "-" ? "-" : "+";
    const n = +(side === "-" ? dline.dataset.a : dline.dataset.b);
    if (!n) return;
    const repeat = STATE.blameLine === n && STATE.blameSide === side;
    STATE.blameLine = repeat ? null : n;
    STATE.blameSide = side;
    // This row is the line as it stood in the state being inspected.
    STATE.blameText = rowText(dline);
    renderMain();
    return;
  }

  const row = evt.target.closest("#split .urow[data-ln]");
  if (!row || STATE.mode !== "src") return;
  const line = +row.dataset.ln;
  STATE.srcLine = STATE.srcLine === line ? null : line;
  // Scroll the *other* pane: the side you clicked is already where you want it.
  applySrcHighlight(row.closest(".cmapside") ? "irmap" : "cmapside");
});

// Status strip: a badge opens the drawer on that tab, anywhere else toggles it.
document.getElementById("strip").addEventListener("click", evt => {
  const badge = evt.target.closest(".strip-badge");
  if (badge) {
    const same = STATE.bottomOpen && STATE.bottomTab === badge.dataset.tab;
    STATE.bottomTab = badge.dataset.tab;
    STATE.bottomOpen = !same;
  } else {
    STATE.bottomOpen = !STATE.bottomOpen;
  }
  renderBottom();
  resizeGraphs();  // the split view just took (or gave back) the drawer's height
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
  if (STATE.mode === "structure") renderMain();
});
document.getElementById("fnFilter").addEventListener("input", renderFnList);
document.getElementById("passFilter").addEventListener("input", renderPassList);

// The spine is a second way into the same list.
document.getElementById("spine").addEventListener("click", evt => {
  const tick = evt.target.closest(".tick[data-id]");
  if (tick) selectPass(+tick.dataset.id);
});

// Step without leaving the keyboard: j/k through passes, [/] through functions.
document.addEventListener("keydown", evt => {
  if (evt.metaKey || evt.ctrlKey || evt.altKey) return;
  if (evt.target.closest("input, textarea")) return;
  const step = (list, current, delta) => {
    const i = list.indexOf(current);
    return list[Math.min(list.length - 1, Math.max(0, (i < 0 ? 0 : i + delta)))];
  };
  if (evt.key === "j" || evt.key === "k") {
    const ids = listedPasses().map(p => p.id);
    const next = step(ids, STATE.passId, evt.key === "j" ? 1 : -1);
    if (next != null && next !== STATE.passId) selectPass(next);
  } else if (evt.key === "[" || evt.key === "]") {
    const next = step(fnNames(), STATE.fn, evt.key === "]" ? 1 : -1);
    if (next && next !== STATE.fn) selectFn(next);
  } else {
    return;
  }
  evt.preventDefault();
});

window.addEventListener("DOMContentLoaded", boot);
