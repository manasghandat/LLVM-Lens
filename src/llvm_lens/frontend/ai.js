/* ask AI: the report's help panel -- configuration, context assembly, provider
 * clients and the drawer UI.
 *
 * A classic script loaded after app.js, so it shares that file's global
 * lexical scope and can read the live view state (STATE, CURRENT_MANIFEST,
 * CURRENT_PASS) and reuse its helpers (escapeHtml, highlightIR, diffStat,
 * diffHunks, sourceFiles) rather than re-deriving any of it.
 *
 * The report is a static file:// page with no backend, so the browser talks to
 * the provider directly. Credentials are configured once with
 * `llvm-lens configure-ai` (~/.llvm_lens_config), copied into the report as the
 * gitignored data/ai-config.* sidecar, and seeded into localStorage from there
 * -- nothing secret reaches index.html, app.js or manifest.json.
 *
 * Token discipline is the point of the design: the system prompt is short and
 * fixed, one bounded digest of the current selection rides on the newest
 * message only (never on replayed history), and both the diff and source
 * excerpts are capped with an explicit truncation marker. */

"use strict";

/* --- ask ai: config -------------------------------------------------------- */

const AI_STORE_KEY = "llvm-lens.ai";
const AI_SIDECAR = "ai-config";

// The wire protocols the panel speaks. "openai-compatible" covers OpenRouter,
// Ollama, LM Studio, vLLM and any proxy in front of OpenAI's own host, whose
// endpoint refuses browser CORS on its own.
const AI_DEFAULTS = {
  anthropic: { model: "claude-opus-5", baseUrl: "https://api.anthropic.com" },
  "openai-compatible": { model: "", baseUrl: "https://api.openai.com/v1" },
};
const AI_PROVIDERS = [
  { id: "anthropic", label: "Anthropic" },
  { id: "openai-compatible", label: "OpenAI-compatible" },
];

// Output ceiling, not a target: answers are meant to be short. It has to be
// generous anyway, because current Claude models run adaptive thinking by
// default and thinking is billed against this same budget -- at a low cap a
// hard question can spend the whole allowance thinking and come back with no
// text block at all, which reads as "the provider returned no text".
const AI_MAX_TOKENS = 16000;
const AI_HISTORY_MAX = 8;        // messages of history kept (context is re-sent)
const AI_LIST_FNS = 24;          // functions listed before it becomes a count

// Per-question context budget. "trimmed" is what a question attaches by
// default; "full" is what the user opts into when the panel says the selection
// does not fit. A pass that rewires a function produces 100-450 diff lines for
// an ordinary function, so the default has to clear that -- prompting on a
// question that fits would just teach the user to click through the prompt.
// The two caps per section are kept in step (an IR line runs ~50 characters),
// so neither one silently dominates the other.
const AI_CAPS = {
  trimmed: { diffLines: 600, diffChars: 32000, srcLines: 60, logLines: 200, logChars: 12000 },
  // Past this, a single function's diff is beyond anything worth one request,
  // and the panel says so rather than cutting quietly.
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

// Fill in whatever the stored config left out, so the rest of the file can
// read cfg.model / cfg.baseUrl without repeating the fallbacks.
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

// localStorage can throw outright (private windows, blocked site data), so
// every access is guarded and a failure just means "not configured here yet".
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

// The build-time sidecar: fetch first, then the .js wrapper (browsers block
// fetch on file://). Same bargain app.js's loadJSON makes for the pass chunks.
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
    // A script that neither loads nor errors (blocked, or a file:// fetch that
    // dies silently) would leave the panel initialising forever; give up and
    // report "not configured" instead.
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

// Precedence: the report's own build-time copy wins, because re-running
// `llvm-lens configure-ai` and rebuilding is the documented way to change the
// key -- if a stale browser copy won instead, that command would silently do
// nothing for anyone who had ever typed a key into the panel.
//
// Nothing is written to localStorage on the way in. Seeding it from the
// sidecar is what made `configure-ai --clear` look like it did nothing: the
// browser kept a copy of a key the user had deliberately removed, and it
// outlived both the config file and the report. Now the browser copy exists
// only when someone typed one into the panel, and `forget` below removes it.
async function loadAIConfig() {
  AI_CONFIG = readStoredConfig();
  AI_CONFIG_SOURCE = AI_CONFIG ? "browser" : null;
  // Instant for a browser copy, so the panel is usable while the sidecar's
  // script tag (or its timeout) is still pending.
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

// Stable and short: it is re-sent on every request, so every line here is a
// recurring cost. It carries the report's vocabulary (so the model knows where
// to point) and the answering rules (so it stops at the answer).
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
- A pass card carries before/after snapshots per function; Changes is lines added/removed. Some
  cards also carry spills (stack traffic), a regmap (virtual to physical register, from the
  register allocator), analyses run/invalidated, and a log (that pass's own stderr).
- "[module]" is the whole-module pseudo-row, not a function.
- Views: Diff (what this pass touched), IR (whole bodies), CFG, Source (IR/MIR line to C line,
  needs debug info), ISel (IR to machine IR at instruction selection), Graphs (module analyses),
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

// What a section would cost to send whole, and whether the budget can take it.
// The panel turns `over` into "this is large -- send it anyway?", which is the
// difference between a user choosing to spend the tokens and us cutting the
// evidence out from under an answer that then has to guess.
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

// A plain-text unified diff of the selected function's change, hunks only --
// the model reads text, and the hunk headers carry the line numbers it needs
// to cite. Whole bodies are deliberately not attached.
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

// Which file the selected snapshot mostly came from, and the window of it the
// mapped lines cover -- inlining can pull in several files, so this picks the
// dominant one rather than assuming.
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

// The digest attached to one question, and what did not fit into it. It rides
// on the newest message only (buildMessages), so replaying history does not
// re-pay for it.
//
// `cuts` is the part that matters to the user: a section listed there was
// dropped or shortened, and the panel asks before sending it that way rather
// than letting an answer run out of evidence halfway through.
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

// "the diff of main is 312 lines, more than the 80 that fit" -- said before
// the question is sent, not discovered by the model afterwards.
function cutLabels(cuts) { return cuts.map(cut => cut.label).join(", "); }
function cutSizes(cuts) {
  return cuts.map(cut => `${cut.lines} lines (${Math.round(cut.chars / 1024)} kB)`).join("; ");
}
function cutSummary(cuts) {
  return cuts.map(cut => `${cut.label} is ${cut.lines} lines`
    + ` (${Math.round(cut.chars / 1024)} kB)`).join("; ");
}

// Cutting the selection is the user's call, not ours: ask before sending a
// question whose evidence would arrive half-missing. A question already
// answered once (mode set) is sent without asking again.
function needsConsent(digest, mode) {
  return Boolean(digest.cuts.length) && !mode;
}

// The context rides on the newest message only; older turns keep just the
// question, which is what makes a long conversation affordable.
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

// The exact fetch arguments for one request, kept pure so the shape can be
// pinned by tests without a network.
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
  // Anthropic's browser CORS gate is off by default; the header below is what
  // makes a direct call from this page legal. The key rides in x-api-key, not
  // in an Authorization header.
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
    // A reasoning model puts its answer in content and its scratch work in
    // reasoning_content; some servers (and some proxies) fill only the latter,
    // which would otherwise read as an empty reply.
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

// Some endpoints stream whatever the request asked for, so the reply arrives
// as a run of SSE frames rather than one JSON body. Both wire formats put the
// newest text in a delta.
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

// Why an empty answer is empty. A turn cut off at max_tokens, a refusal, and a
// model that ran out of context are different problems, and the finish reason
// is the only place that difference is recorded.
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

// Providers report failures in their own shape; fall back to the raw body,
// which is the only thing that helps when a proxy returns HTML.
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
    // A blocked preflight lands here as an opaque TypeError: in a browser the
    // endpoint is unreachable from this page, usually for CORS.
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
    // Several very different causes arrive here as the same empty body, so
    // name which one this is rather than leaving the user to guess.
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

// Answer text -> HTML. Fenced blocks are the interesting part (IR/MIR
// snippets) and get the report's own tokenizer; everything else is escaped
// text with inline code spans.
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

// The "this is bigger than what I'd send" prompt. It is not an error and not a
// question for the AI -- it is the panel refusing to send a silently cut
// selection and asking which one the user wants.
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

// The numbers, not the text: a question sent with a cut selection says so, so
// a short answer is not mistaken for the whole story. A "full" send that was
// still cut says that instead, rather than claiming it went whole.
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

// What this question will carry, so the user can see the AI's eye before
// sending rather than guessing why it answered about the wrong function.
function renderAIContext() {
  const el = aiEl("aiCtx");
  if (!el) return;
  const summary = aiPassSummary();
  const fn = (typeof STATE !== "undefined" && STATE.fn) || null;
  // A held question is about the selection it was asked of. Once the user
  // moves on, answering it would send the new selection's context under the
  // old question, so the notice goes with the selection.
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
  // Saying *which* copy is in force is what makes a key the user thought they
  // had removed visible again, instead of it quietly keeping the panel alive.
  // And when the two disagree, saying that too: the build's copy wins, so a key
  // typed here would otherwise look like it had been ignored.
  el.textContent = !mask ? "not set"
    : AI_CONFIG_SOURCE === "report"
      ? `using this report's build · ${mask}`
        + (stored && stored.apiKey && stored.apiKey !== cfg.apiKey
          ? " · this browser has a different key" : "")
      : `set in this browser · ${mask}`;
  // Offered whenever a browser copy exists, even if the build's copy is the one
  // in force -- otherwise a stale key would be unreachable from the UI.
  const forget = aiEl("aiForget");
  if (forget) forget.hidden = !stored;
  // And it is only when there are *two* copies that forgetting is not the whole
  // story, so the caveat sits beside the button rather than on its own.
  const note = aiEl("aiForgetNote");
  if (note) note.hidden = !(stored && AI_CONFIG_SOURCE === "report");
}

// Drop the browser's own copy. Only that copy -- the report's build-time one
// lives in the report directory, which a static page cannot reach.
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

// *mode* is undefined from the composer, or "trimmed"/"full" from the notice
// the composer raises when the selection does not fit.
async function sendAI(question, mode) {
  const input = aiEl("aiPrompt");
  // The composer sends whatever is typed; the notice re-sends a question it is
  // already holding. Only the composer's own text gets taken, so answering a
  // notice cannot eat a new question half-typed behind it.
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
  // Cutting a large selection is the user's call, not ours: say what does not
  // fit and let them decide, rather than sending half the evidence and letting
  // the answer trail off at the cut.
  if (needsConsent(digest, mode)) {
    // Whether "send the full context" can actually hold it, decided here and
    // not at click time, so the prompt never promises a complete send it
    // cannot make.
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
  // Opening an unconfigured panel lands on the settings, which is the only
  // thing that can make it work.
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
  // Saving with the field blank removes the browser's copy; anything else
  // becomes one. Either way what is in force is the browser's, not the build's.
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
  // Redraw the form from what was just stored: a blank key falls back to the
  // build's copy, and the note has to say so rather than keep claiming "not set".
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

  // The notice is re-rendered on every message render, so its buttons are
  // delegated rather than bound.
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
    // Enter sends, Shift+Enter is a newline -- the usual bargain for a
    // one-line composer, and it keeps a multi-line question reachable.
    if (evt.key === "Enter" && !evt.shiftKey) {
      evt.preventDefault();
      sendAI();
    }
  });

  document.addEventListener("keydown", evt => {
    if (evt.key === "Escape" && !aiEl("aiPanel").hidden) toggleAIPanel(false);
  });

  // The attached context follows the view, so the strip stays honest as the
  // user moves between passes, functions and modes. Every one of those paths
  // -- including boot's initial auto-select -- ends in renderMain(), and a
  // top-level function declaration in a classic script is a writable property
  // of the global object, so wrapping it here catches them all.
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
  // After app.js's boot has the manifest; failures here must not take the
  // report down with them.
  Promise.resolve()
    .then(() => (typeof manifestPromise !== "undefined" ? manifestPromise : null))
    .then(initAI)
    .catch(err => {
      const box = document.getElementById("aiMessages");
      if (box) box.innerHTML = `<div class="ai-err">ask AI unavailable: ${escapeHtml(err.message || String(err))}</div>`;
    });
});
