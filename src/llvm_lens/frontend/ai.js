/* ask AI: config resolution, context assembly, provider clients, drawer UI. */

"use strict";

/* --- ask ai: config -------------------------------------------------------- */

const AI_STORE_KEY = "llvm-lens.ai";
const AI_SIDECAR = "ai-config";

const AI_DEFAULTS = {
  anthropic: { model: "claude-opus-5", baseUrl: "https://api.anthropic.com" },
  "openai-compatible": { model: "", baseUrl: "https://api.openai.com/v1" },
};
const AI_PROVIDERS = [
  { id: "anthropic", label: "Anthropic" },
  { id: "openai-compatible", label: "OpenAI-compatible" },
];

const AI_MAX_TOKENS = 16000;
const AI_HISTORY_MAX = 8;        // messages of history kept (context is re-sent)
const AI_LIST_FNS = 24;          // functions listed before it becomes a count

const AI_CAPS = {
  trimmed: { diffLines: 600, diffChars: 32000, srcLines: 60, logLines: 200, logChars: 12000 },
  full: { diffLines: 6000, diffChars: 400000, srcLines: 2000, logLines: 2000, logChars: 120000 },
};
// What a default (trimmed) question attaches -- the numbers the tests pin.
const AI_DIFF_LINES = AI_CAPS.trimmed.diffLines;
const AI_SRC_LINES = AI_CAPS.trimmed.srcLines;

let AI_CONFIG = null;            // {provider, api_key, model, base_url}
let AI_CONFIG_SOURCE = null;     // "report" (this build's sidecar) | "browser"
let AI_BUILT = null;             // the sidecar's copy alone, kept so `forget`
                                 // can fall back to it instead of forgetting both
let AI_HISTORY = [];             // [{role, content, cuts?}] -- questions and answers
let AI_PENDING = false;          // a request is in flight
let AI_ERROR = null;             // last error, shown above the composer
let AI_NOTICE = null;            // {question, cuts} -- asked but not yet sent

function aiDefaults(provider) {
  return AI_DEFAULTS[provider] || AI_DEFAULTS.anthropic;
}

function normalizeAIConfig(raw) {
  if (!raw) return null;
  const provider = AI_PROVIDERS.some(p => p.id === raw.provider)
    ? raw.provider : "anthropic";
  const defaults = aiDefaults(provider);
  return {
    provider,
    apiKey: (raw.api_key || "").trim(),
    model: (raw.model || "").trim() || defaults.model,
    baseUrl: (raw.base_url || "").trim() || defaults.baseUrl,
  };
}

function readStoredConfig() {
  try {
    return normalizeAIConfig(JSON.parse(localStorage.getItem(AI_STORE_KEY)));
  } catch {
    return null;
  }
}

function storeConfig(cfg) {
  try {
    localStorage.setItem(AI_STORE_KEY, JSON.stringify({
      provider: cfg.provider,
      api_key: cfg.apiKey,
      model: cfg.model,
      base_url: cfg.baseUrl,
    }));
  } catch {
    /* storage unavailable: the panel works for this page load regardless */
  }
}

function clearStoredConfig() {
  try {
    localStorage.removeItem(AI_STORE_KEY);
  } catch { /* nothing to clear */ }
}

async function loadSidecarConfig() {
  try {
    const response = await fetch(`data/${AI_SIDECAR}.json`);
    if (response.ok) return normalizeAIConfig(await response.json());
  } catch { /* fall through to the script wrapper */ }
  if (typeof window !== "undefined" && window.__LLVM_LENS_AI_CONFIG__) {
    return normalizeAIConfig(window.__LLVM_LENS_AI_CONFIG__);
  }
  return new Promise(resolve => {
    const script = document.createElement("script");
    const timer = setTimeout(() => resolve(null), 5000);
    script.src = `data/${AI_SIDECAR}.js`;
    script.onload = () => {
      clearTimeout(timer);
      resolve(normalizeAIConfig(window.__LLVM_LENS_AI_CONFIG__));
    };
    script.onerror = () => {
      clearTimeout(timer);
      resolve(null);
    };
    document.head.appendChild(script);
  });
}

async function loadAIConfig() {
  AI_CONFIG = readStoredConfig();
  AI_CONFIG_SOURCE = AI_CONFIG ? "browser" : null;
  AI_BUILT = await loadSidecarConfig();
  if (AI_BUILT) {
    AI_CONFIG = AI_BUILT;
    AI_CONFIG_SOURCE = "report";
  }
  return AI_CONFIG;
}

function currentAIConfig() { return AI_CONFIG; }
function isConfigured() { return !!(AI_CONFIG && AI_CONFIG.apiKey); }

/* --- ask ai: prompt -------------------------------------------------------- */

const AI_SYSTEM_PROMPT = `You are the assistant inside an LLVM-Lens report: a static page showing
what each LLVM pass did to one translation unit. The user is a compiler engineer looking at a
specific pass right now.

Answer:
- Be terse. Two or three sentences unless asked for more. No preamble, no restating the question
  or the attached context, no describing what you are about to do.
- Name things exactly as the report does: the pass by number and name ("#31 SROAPass"), the
  function, and the block or line as the context prints them.
- Quote only the lines that matter. Never re-print the attached context.
- The attached context is what the user is looking at. If the answer needs something it does not
  carry -- another pass, another function, a full body, the pipeline order -- say which pass,
  function or view to open instead of guessing, in one sentence.
- A section ending in "… (truncated)" was cut. If the question needs what is past the cut, say
  so on its own line and stop -- never describe the cut part from assumption, and never say the
  answer "cannot be enumerated" without telling the user to re-send with the full context.
- Never invent pass names, instructions or line numbers. If the context does not show it, say so.

What the report holds (so you know where to point):
- Two lanes. IR is the opt middle-end pipeline; machine is the llc backend. Each lane opens with
  an input card ("Input IR" / "Optimized IR") holding the whole module that lane was handed.
  The machine lane closes with "Optimized MIR": every function's machine IR after the last llc pass.
- A pass card carries before/after snapshots per function; Changes is lines added/removed. Some
  cards also carry spills (stack traffic), a regmap (virtual to physical register, from the
  register allocator), analyses run/invalidated, and a log (that pass's own stderr).
- "[module]" is the whole-module pseudo-row, not a function.
- Views: Diff (what this pass touched), IR (whole bodies), CFG, Source (IR/MIR line to C line,
  needs debug info), Asm (final machine IR beside the assembly, instruction by instruction, on the last machine cards), ISel (IR to machine IR at instruction selection), Graphs (module analyses),
  Structure (the pass-manager tree).`;

function buildSystemPrompt() {
  return AI_SYSTEM_PROMPT;
}

function truncate(text, maxChars, maxLines) {
  let out = String(text == null ? "" : text);
  const lines = out.split("\n");
  let cut = false;
  if (lines.length > maxLines) {
    out = lines.slice(0, maxLines).join("\n");
    cut = true;
  }
  if (out.length > maxChars) {
    out = out.slice(0, maxChars);
    cut = true;
  }
  return cut ? `${out}\n… (truncated)` : out;
}

function fitInfo(text, maxChars, maxLines) {
  const body = String(text == null ? "" : text);
  const lines = body ? body.split("\n").length : 0;
  return {
    lines, chars: body.length,
    over: lines > maxLines || body.length > maxChars,
  };
}

function aiMeta() {
  return ((CURRENT_MANIFEST || {}).metadata) || {};
}

function aiPassSummary() {
  return typeof currentPassSummary === "function" ? currentPassSummary() : null;
}

function diffText(change) {
  const ops = diffStat(change.before, change.after).ops;
  const hunks = diffHunks(ops, 3);
  if (!hunks.length) return "(no textual change)";
  return hunks.map(hunk => {
    const rows = hunk.rows.map(row => `${row.op}${row.text}`).join("\n");
    const hidden = hunk.hidden ? ` (${hunk.hidden} unchanged lines hidden)` : "";
    return `${hunk.header}${hidden}\n${rows}`;
  }).join("\n");
}

function sourceExcerpt(change, maxLines) {
  const files = sourceFiles();
  const map = (change && change.srcAfter) || [];
  if (!files.length || !map.length) return null;
  const tally = new Map();
  for (const ref of map) if (ref) tally.set(ref[0], (tally.get(ref[0]) || 0) + 1);
  if (!tally.size) return null;
  const [index] = [...tally.entries()].sort((a, b) => b[1] - a[1])[0];
  const file = files[index];
  if (!file) return null;
  const covered = map.filter(r => r && r[0] === index).map(r => r[1]);
  const start = Math.max(1, Math.min(...covered));
  const all = splitLines(file.text);
  const lines = all.slice(start - 1, start - 1 + (maxLines || AI_SRC_LINES));
  return {
    name: file.name,
    start,
    end: start + lines.length - 1,
    total: all.length,
    text: lines.map((t, i) => `${start + i}: ${t}`).join("\n"),
  };
}

function functionList(summary, pass) {
  const names = (summary && summary.functions) || [];
  const changed = names.filter(n => n !== "[module]" && pass
    && pass.functions && pass.functions[n] && pass.functions[n].changed);
  const shown = changed.length ? changed : names.filter(n => n !== "[module]");
  const head = shown.slice(0, AI_LIST_FNS);
  const rest = shown.length - head.length;
  const line = head.join(", ") + (rest > 0 ? ` … +${rest} more` : "");
  return `${changed.length} changed of ${names.length}: ${line || "(none)"}`;
}

function contextDigest(opts) {
  const caps = (opts && opts.full) ? AI_CAPS.full : AI_CAPS.trimmed;
  const cuts = [];
  const meta = aiMeta();
  const summary = aiPassSummary();
  const pass = typeof CURRENT_PASS !== "undefined" ? CURRENT_PASS : null;
  const fn = (typeof STATE !== "undefined" && STATE.fn) || null;
  const parts = [];

  const tools = [...new Set(Object.values(meta.toolVersions || {})
    .map(v => String(v).replace(/\s*\(.*\)$/, "")))].join(" · ");
  parts.push([
    "=== report ===",
    `source: ${meta.source || "(unknown)"} (${meta.inputKind || "?"})`,
    `pipeline: ${meta.pipeline || "(unknown)"}`,
    tools ? `tools: ${tools}` : "",
  ].filter(Boolean).join("\n"));

  if (summary) {
    const delta = summary.lineDelta;
    parts.push([
      `=== selected pass: #${String(summary.runIndex).padStart(3, "0")} ${summary.name} ===`,
      `lane: ${summary.lane === "mir" ? "machine (llc)" : "IR (opt)"}`
        + `  scope: ${summary.scope || "-"}`
        + `  changed: ${summary.changed ? "yes" : "no"}`,
      delta ? `changes: +${delta.added} -${delta.removed} lines` : "",
      summary.spillCount ? `spills: ${summary.spillCount}` : "",
      `functions: ${functionList(summary, pass)}`,
      summary.analysisCounts
        ? `analyses: run ${summary.analysisCounts.run || 0},`
          + ` invalidated ${summary.analysisCounts.invalidated || 0}`
        : "",
      summary.isInput ? "this is an input card (whole module, no diff, no CFG)" : "",
      summary.isOutput ? "this is the output card (final machine IR per function, no diff)" : "",
    ].filter(Boolean).join("\n"));
  } else {
    parts.push("=== no pass selected ===");
  }

  const change = fn && typeof fnChange === "function" ? fnChange(fn) : null;
  if (fn && change) {
    const head = [`=== selected function: ${fn} ===`,
      `status: ${change.changed ? "changed by this pass" : "untouched by this pass"}`];
    if (pass && pass.spills && pass.spills[fn]) head.push(`spills: ${pass.spills[fn]}`);
    parts.push(head.join("\n"));
    if (change.changed) {
      const diff = diffText(change);
      const fit = fitInfo(diff, caps.diffChars, caps.diffLines);
      if (fit.over) cuts.push({ label: `the diff of ${fn}`, lines: fit.lines, chars: fit.chars });
      parts.push(`--- diff (unified, 3 lines of context) ---\n`
        + truncate(diff, caps.diffChars, caps.diffLines));
    }
    const src = sourceExcerpt(change, caps.srcLines);
    if (src) {
      parts.push(`--- source: ${src.name} lines ${src.start}-${src.end}`
        + ` of ${src.total} ---\n` + src.text);
    }
  } else if (fn) {
    parts.push(`=== selected function: ${fn} ===\n(snapshot not loaded)`);
  }

  if (pass && pass.log) {
    const log = pass.log.trim();
    if (log) {
      const fit = fitInfo(log, caps.logChars, caps.logLines);
      if (fit.over) cuts.push({ label: "the pass log", lines: fit.lines, chars: fit.chars });
      parts.push(`--- pass log (stderr) ---\n` + truncate(log, caps.logChars, caps.logLines));
    }
  }
  return { text: parts.join("\n\n"), cuts };
}

// The digest as plain text, for callers that only want to send it.
function buildContext(opts) {
  return contextDigest(opts).text;
}

function cutLabels(cuts) { return cuts.map(cut => cut.label).join(", "); }
function cutSizes(cuts) {
  return cuts.map(cut => `${cut.lines} lines (${Math.round(cut.chars / 1024)} kB)`).join("; ");
}
function cutSummary(cuts) {
  return cuts.map(cut => `${cut.label} is ${cut.lines} lines`
    + ` (${Math.round(cut.chars / 1024)} kB)`).join("; ");
}

function needsConsent(digest, mode) {
  return Boolean(digest.cuts.length) && !mode;
}

function buildMessages(history, contextBlock, question) {
  const recent = history.slice(-AI_HISTORY_MAX);
  const messages = recent.map(m => ({ role: m.role, content: m.content }));
  messages.push({
    role: "user",
    content: `${contextBlock}\n\n=== question ===\n${question}`,
  });
  return messages;
}

/* --- ask ai: providers ----------------------------------------------------- */

function aiEndpoint(cfg, path) {
  return `${cfg.baseUrl.replace(/\/+$/, "")}${path}`;
}

function buildRequest(cfg, system, messages, maxTokens = AI_MAX_TOKENS) {
  if (cfg.provider === "openai-compatible") {
    return {
      url: aiEndpoint(cfg, "/chat/completions"),
      headers: {
        "content-type": "application/json",
        "authorization": `Bearer ${cfg.apiKey}`,
      },
      body: {
        model: cfg.model,
        max_tokens: maxTokens,
        messages: [{ role: "system", content: system }, ...messages],
      },
    };
  }
  return {
    url: aiEndpoint(cfg, "/v1/messages"),
    headers: {
      "content-type": "application/json",
      "x-api-key": cfg.apiKey,
      "anthropic-version": "2023-06-01",
      "anthropic-dangerous-direct-browser-access": "true",
    },
    body: {
      model: cfg.model,
      max_tokens: maxTokens,
      system,
      messages,
    },
  };
}

// Pull the assistant's text out of whichever envelope came back.
function extractText(provider, data) {
  if (provider === "openai-compatible") {
    const choice = (data.choices || [])[0] || {};
    const message = choice.message || {};
    for (const key of ["content", "reasoning_content", "reasoning"]) {
      const value = message[key];
      if (typeof value === "string" && value.trim()) return value;
      if (Array.isArray(value)) {
        const joined = value.map(c => (typeof c === "string" ? c : c.text || "")).join("");
        if (joined.trim()) return joined;
      }
    }
    return "";
  }
  return (data.content || [])
    .filter(block => block.type === "text")
    .map(block => block.text)
    .join("");
}

function parseSSE(raw) {
  const out = [];
  for (const line of String(raw).split(/\r?\n/)) {
    if (!line.startsWith("data:")) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    try {
      const frame = JSON.parse(payload);
      const delta = ((frame.choices || [])[0] || {}).delta || {};
      if (typeof delta.content === "string") out.push(delta.content);
      if (frame.type === "content_block_delta" && frame.delta
          && typeof frame.delta.text === "string") {
        out.push(frame.delta.text);
      }
    } catch { /* an unreadable frame is not worth failing the whole reply over */ }
  }
  return out.join("");
}

function finishReason(provider, data) {
  const choice = (data.choices || [])[0] || {};
  return (provider === "openai-compatible" ? choice.finish_reason : data.stop_reason) || "";
}

function finishNote(provider, data) {
  const choice = (data.choices || [])[0] || {};
  const reason = finishReason(provider, data);
  const blocks = provider === "openai-compatible"
    ? Object.keys(choice.message || {}).join(", ")
    : (data.content || []).map(block => block.type).join(", ");
  return [
    reason ? `stop_reason: ${reason}` : "",
    `content: ${blocks || "empty"}`,
  ].filter(Boolean).join("; ");
}

function extractError(provider, status, data, raw) {
  const message = data && data.error && (data.error.message || data.error.type);
  if (message) return `${status}: ${message}`;
  const text = (raw || "").trim();
  return `${status}: ${text ? text.slice(0, 300) : "request failed"}`;
}

async function askAI(cfg, system, messages, signal) {
  const request = buildRequest(cfg, system, messages);
  let response;
  try {
    response = await fetch(request.url, {
      method: "POST",
      headers: request.headers,
      body: JSON.stringify(request.body),
      signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new Error(
      `could not reach ${request.url}. Check the base URL, the network, and `
      + "whether the provider allows browser requests (OpenAI's own endpoint "
      + "does not -- use OpenRouter, a local server, or a proxy).");
  }
  const raw = await response.text();
  let data = null;
  try { data = JSON.parse(raw); } catch { /* not a JSON body */ }
  if (!response.ok) {
    throw new Error(extractError(cfg.provider, response.status, data, raw));
  }
  let text = "", why = "";
  if (data) {
    text = extractText(cfg.provider, data).trim();
    why = finishNote(cfg.provider, data);
  } else {
    // A body that will not parse is usually a streamed reply.
    text = parseSSE(raw).trim();
  }
  if (!text) {
    const reason = data ? finishReason(cfg.provider, data) : "";
    const cause = !data ? "the response body was neither JSON nor SSE frames"
      : reason === "max_tokens"
        ? "it ran out of output budget before writing an answer -- ask something"
          + " narrower, or raise max_tokens"
      : reason === "refusal" ? "the model refused to answer"
      : "check that the model name is one this endpoint serves";
    throw new Error("the provider returned no text"
      + (why ? ` (${why})` : "") + ` -- ${cause}`);
  }
  return text;
}

/* --- ask ai: ui ------------------------------------------------------------ */

function aiEl(id) { return document.getElementById(id); }

function aiProviderLabel(provider) {
  const found = AI_PROVIDERS.find(p => p.id === provider);
  return found ? found.label : provider;
}

function renderAIModel() {
  const el = aiEl("aiModel");
  if (!el) return;
  if (!isConfigured()) { el.textContent = "not configured"; return; }
  el.textContent = `${aiProviderLabel(AI_CONFIG.provider)} · ${AI_CONFIG.model}`;
  el.title = `endpoint: ${AI_CONFIG.baseUrl}`;
}

function renderAnswerHtml(text) {
  const parts = String(text).split(/```/);
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[a-zA-Z0-9_+-]*\n/, "");
      return `<pre class="ai-code"><code>${highlightIR(body)}</code></pre>`;
    }
    const escaped = escapeHtml(part)
      .replace(/`([^`\n]+)`/g, '<code class="ai-inline">$1</code>');
    return escaped.replace(/\n/g, "<br>");
  }).join("");
}

function renderAINotice() {
  if (!AI_NOTICE) return "";
  const beyond = AI_NOTICE.stillCut;
  return `
    <div class="ai-ask">
      <div class="ai-ask-title">This selection is larger than a question usually carries</div>
      <div class="ai-ask-note">Sent trimmed it cuts ${escapeHtml(cutLabels(AI_NOTICE.cuts))}
        — ${escapeHtml(cutSizes(AI_NOTICE.cuts))} — and the answer stops where
        the context does. ${beyond.length
          ? `Sent whole it still cuts ${escapeHtml(cutLabels(beyond))}; expect
             the answer to stop there too.`
          : "Sent whole, the answer can reach the end."}</div>
      <div class="ai-ask-row">
        <button type="button" class="ai-ask-btn go" data-aisend="full">${
          beyond.length ? "send it anyway" : "send the full context"}</button>
        <button type="button" class="ai-ask-btn" data-aisend="trimmed">send it trimmed</button>
        <button type="button" class="ai-ask-btn" data-aisend="cancel">cancel</button>
      </div>
    </div>`;
}

function cutBadge(m) {
  const cuts = m.cuts;
  if (!cuts || !cuts.length) return "";
  const what = m.mode === "full" ? "sent whole, still cut at" : "sent trimmed";
  return `<div class="ai-cut">${what} — ${escapeHtml(cutSummary(cuts))}</div>`;
}

function renderAIMessages() {
  const box = aiEl("aiMessages");
  if (!box) return;
  const rows = AI_HISTORY.map(m => `
    <div class="ai-msg ${m.role === "user" ? "me" : "bot"}">
      <div class="ai-role">${m.role === "user" ? "you" : "ai"}</div>
      <div class="ai-text">${m.role === "user"
        ? escapeHtml(m.content).replace(/\n/g, "<br>")
        : renderAnswerHtml(m.content)}</div>
      ${m.role === "user" ? cutBadge(m) : ""}
    </div>`);
  if (!AI_HISTORY.length && !AI_ERROR && !AI_NOTICE) {
    rows.push(`<div class="ai-hint">${isConfigured()
      ? "Ask about the selected pass or function. It reads what the panes show."
      : "No API key configured. Run <code>llvm-lens configure-ai</code>, then "
        + "rebuild the report — or set a key here with ⚙ (this browser only)."}
    </div>`);
  }
  if (AI_PENDING) rows.push('<div class="ai-msg bot"><div class="ai-text ai-wait">…</div></div>');
  const error = AI_ERROR
    ? `<div class="ai-err">${escapeHtml(AI_ERROR)}</div>` : "";
  box.innerHTML = rows.join("") + renderAINotice() + error;
  box.scrollTop = box.scrollHeight;
}

function renderAIContext() {
  const el = aiEl("aiCtx");
  if (!el) return;
  const summary = aiPassSummary();
  const fn = (typeof STATE !== "undefined" && STATE.fn) || null;
  if (AI_NOTICE && (AI_NOTICE.passId !== (typeof STATE !== "undefined" ? STATE.passId : null)
      || AI_NOTICE.fn !== fn)) {
    AI_NOTICE = null;
    renderAIMessages();
  }
  if (!summary) { el.textContent = "no pass selected"; return; }
  const bits = [`pass #${String(summary.runIndex).padStart(3, "0")} ${summary.name}`];
  if (fn) bits.push(fn);
  el.textContent = `context: ${bits.join(" · ")}`;
}

function renderAISettings() {
  const form = aiEl("aiSettings");
  if (!form) return;
  const cfg = AI_CONFIG || normalizeAIConfig({ provider: "anthropic" });
  form.elements.provider.value = cfg.provider;
  form.elements.model.value = cfg.model;
  form.elements.baseUrl.value = cfg.baseUrl;
  form.elements.apiKey.value = cfg.apiKey || "";
  renderAIKeyNote();
  renderAIBaseUrlRow();
}

function renderAIKeyNote() {
  const el = aiEl("aiKeyNote");
  if (!el) return;
  const cfg = AI_CONFIG;
  const mask = cfg && cfg.apiKey ? `${"•".repeat(8)}${cfg.apiKey.slice(-4)}` : "";
  const stored = readStoredConfig();
  el.textContent = !mask ? "not set"
    : AI_CONFIG_SOURCE === "report"
      ? `using this report's build · ${mask}`
        + (stored && stored.apiKey && stored.apiKey !== cfg.apiKey
          ? " · this browser has a different key" : "")
      : `set in this browser · ${mask}`;
  const forget = aiEl("aiForget");
  if (forget) forget.hidden = !stored;
  const note = aiEl("aiForgetNote");
  if (note) note.hidden = !(stored && AI_CONFIG_SOURCE === "report");
}

function forgetAIKey() {
  clearStoredConfig();
  AI_CONFIG = AI_BUILT;
  AI_CONFIG_SOURCE = AI_BUILT ? "report" : null;
  AI_ERROR = AI_BUILT ? null
    : "Key forgotten. Run `llvm-lens configure-ai` and rebuild, or set one here.";
  renderAIModel();
  renderAISettings();
  renderAIContext();
  renderAIMessages();
}

// Only the configurable protocol has a base URL worth editing.
function renderAIBaseUrlRow() {
  const row = aiEl("aiBaseUrlRow");
  const form = aiEl("aiSettings");
  if (row && form) row.hidden = form.elements.provider.value !== "openai-compatible";
}

async function sendAI(question, mode) {
  const input = aiEl("aiPrompt");
  const fromComposer = question == null;
  question = ((fromComposer ? (input && input.value) : question) || "").trim();
  if (!question || AI_PENDING) return;

  AI_ERROR = null;
  if (!isConfigured()) {
    AI_ERROR = "No API key configured. Run `llvm-lens configure-ai` and rebuild the "
      + "report, or set one here with ⚙ (stored in this browser only).";
    renderAIMessages();
    return;
  }

  const digest = contextDigest({ full: mode === "full" });
  if (needsConsent(digest, mode)) {
    AI_NOTICE = {
      question, cuts: digest.cuts, stillCut: contextDigest({ full: true }).cuts,
      passId: typeof STATE !== "undefined" ? STATE.passId : null,
      fn: typeof STATE !== "undefined" ? STATE.fn : null,
    };
    renderAIMessages();
    return;
  }
  AI_NOTICE = null;
  if (fromComposer) input.value = "";
  AI_HISTORY.push({
    role: "user", content: question, mode: mode || "trimmed",
    cuts: digest.cuts.length ? digest.cuts : null,
  });
  const messages = buildMessages(AI_HISTORY.slice(0, -1), digest.text, question);
  AI_PENDING = true;
  renderAIMessages();
  try {
    const answer = await askAI(AI_CONFIG, buildSystemPrompt(), messages);
    AI_HISTORY.push({ role: "assistant", content: answer });
  } catch (err) {
    AI_ERROR = (err && err.message) || String(err);
    // The question stays in the transcript so it can be re-sent after the fix.
  } finally {
    AI_PENDING = false;
    renderAIMessages();
    input.focus();
  }
}

// The buttons on the "this is large" notice.
function answerAINotice(what) {
  if (!AI_NOTICE) return;
  const question = AI_NOTICE.question;
  if (what === "cancel") {
    AI_NOTICE = null;
    aiEl("aiPrompt").value = question;
    renderAIMessages();
    return;
  }
  sendAI(question, what === "full" ? "full" : "trimmed");
}

function toggleAIPanel(open) {
  const panel = aiEl("aiPanel");
  const button = aiEl("aiBtn");
  if (!panel) return;
  const show = open === undefined ? panel.hidden : open;
  panel.hidden = !show;
  document.body.classList.toggle("ai-open", show);
  button.classList.toggle("on", show);
  button.setAttribute("aria-expanded", String(show));
  // The main view just changed width under the graphs.
  if (typeof resizeGraphs === "function") resizeGraphs();
  renderAIContext();
  if (show && !isConfigured()) toggleAISettings(true);
}

function toggleAISettings(open) {
  const form = aiEl("aiSettings");
  if (!form) return;
  form.hidden = open === undefined ? !form.hidden : !open;
  if (!form.hidden) renderAISettings();
}

function saveAISettingsFromForm() {
  const form = aiEl("aiSettings");
  AI_CONFIG = normalizeAIConfig({
    provider: form.elements.provider.value,
    model: form.elements.model.value,
    base_url: form.elements.baseUrl.value,
    api_key: form.elements.apiKey.value,
  });
  if (AI_CONFIG.apiKey) {
    storeConfig(AI_CONFIG);
    AI_CONFIG_SOURCE = "browser";
  } else {
    clearStoredConfig();
    AI_CONFIG = AI_BUILT;
    AI_CONFIG_SOURCE = AI_BUILT ? "report" : null;
  }
  AI_ERROR = null;
  renderAIModel();
  renderAISettings();
  renderAIContext();
  renderAIMessages();
}

async function initAI() {
  await loadAIConfig();
  renderAIModel();
  renderAIMessages();
  renderAIContext();

  const button = aiEl("aiBtn");
  if (!button) return;
  button.addEventListener("click", () => toggleAIPanel());
  aiEl("aiClose").addEventListener("click", () => toggleAIPanel(false));
  aiEl("aiSettingsBtn").addEventListener("click", () => toggleAISettings());
  aiEl("aiSend").addEventListener("click", () => sendAI());
  aiEl("aiClear").addEventListener("click", () => {
    AI_HISTORY = [];
    AI_ERROR = null;
    AI_NOTICE = null;
    renderAIMessages();
  });

  aiEl("aiMessages").addEventListener("click", evt => {
    const button = evt.target.closest ? evt.target.closest("[data-aisend]") : null;
    if (button) answerAINotice(button.getAttribute("data-aisend"));
  });

  const form = aiEl("aiSettings");
  form.addEventListener("submit", evt => {
    evt.preventDefault();
    saveAISettingsFromForm();
  });
  aiEl("aiForget").addEventListener("click", () => forgetAIKey());
  form.elements.provider.addEventListener("change", () => {
    // Swapping protocol swaps the endpoint defaults with it.
    form.elements.baseUrl.value = aiDefaults(form.elements.provider.value).baseUrl;
    const model = form.elements.model.value.trim();
    if (!model || AI_PROVIDERS.some(p => aiDefaults(p.id).model === model)) {
      form.elements.model.value = aiDefaults(form.elements.provider.value).model;
    }
    renderAIBaseUrlRow();
  });

  const input = aiEl("aiPrompt");
  input.addEventListener("keydown", evt => {
    if (evt.key === "Enter" && !evt.shiftKey) {
      evt.preventDefault();
      sendAI();
    }
  });

  document.addEventListener("keydown", evt => {
    if (evt.key === "Escape" && !aiEl("aiPanel").hidden) toggleAIPanel(false);
  });

  if (typeof renderMain === "function") {
    const inner = renderMain;
    renderMain = function () {
      const out = inner.apply(this, arguments);
      renderAIContext();
      return out;
    };
  }
}

window.addEventListener("DOMContentLoaded", () => {
  Promise.resolve()
    .then(() => (typeof manifestPromise !== "undefined" ? manifestPromise : null))
    .then(initAI)
    .catch(err => {
      const box = document.getElementById("aiMessages");
      if (box) box.innerHTML = `<div class="ai-err">ask AI unavailable: ${escapeHtml(err.message || String(err))}</div>`;
    });
});
