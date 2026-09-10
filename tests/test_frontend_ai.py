"""The ask-AI panel's browser-free half: config resolution, the system prompt,
what one question actually sends (context assembly and its token caps), and the
two provider request shapes.

Runs frontend/ai.js under node against app.js's *real* diff helpers and IR
tokenizer, so a change to the diff builder that broke the attached context
fails here too. Skipped when node is not installed.

ai.js must keep its sections between "/* --- ask ai: config" and
"/* --- ask ai: ui" marker-delimited; they are what this harness reads.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Shared prelude: app.js's diff section (so ai.js is exercised against the real
# diffStat/diffHunks/splitLines/highlightIR), then ai.js's own sections with the
# handful of globals app.js's lexical scope would give it at runtime stubbed.
# Function declarations from a sloppy direct eval leak into this scope; const
# does not, so anything ai.js declares with const is read through `AICONST`.
PRELUDE = r"""
const fs = require("fs");

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

const appSrc = fs.readFileSync(process.argv[1], "utf8");
const d0 = appSrc.indexOf("/* --- diff helpers");
const d1 = appSrc.indexOf("/* --- views");
if (d0 < 0 || d1 < 0) { console.error("diff section not found"); process.exit(2); }
eval(appSrc.slice(d0, d1));

// The view state ai.js reads out of app.js's scope.
let CURRENT_MANIFEST = null, CURRENT_PASS = null;
const STATE = { fn: null };
let SUMMARY = null, SOURCE_FILES = [];
const currentPassSummary = () => SUMMARY;
const sourceFiles = () => SOURCE_FILES;
function fnChange(fn) {
  return CURRENT_PASS && CURRENT_PASS.functions && CURRENT_PASS.functions[fn];
}

// Storage, network and the DOM, enough for the file:// path: a blocked fetch
// must fall back to injecting data/ai-config.js and reading its global.
const STORE = new Map();
const localStorage = {
  getItem: k => (STORE.has(k) ? STORE.get(k) : null),
  setItem: (k, v) => STORE.set(k, String(v)),
  removeItem: k => STORE.delete(k),
};
const SCRIPTS = [];
const window = { addEventListener: () => {} };
// Enough of an element to read back what the panel says: the settings note
// names which copy of the key is in force, and that label is the only thing
// that makes a key the user thought they had removed visible again.
const ELEMENTS = new Map();
function fakeEl(id) {
  const node = { id, hidden: false, textContent: "", innerHTML: "", value: "",
    title: "", src: "", style: {}, onload: null, onerror: null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, getAttribute: () => null, addEventListener() {},
    appendChild() {}, querySelector: () => null, closest: () => null, focus() {} };
  node.elements = new Proxy({}, { get: (t, k) => t[k] || (t[k] = fakeEl(String(k))) });
  return node;
}
const document = {
  createElement: () => ({}),
  head: { appendChild: el => SCRIPTS.push(el) },
  getElementById(id) {
    if (!ELEMENTS.has(id)) ELEMENTS.set(id, fakeEl(id));
    return ELEMENTS.get(id);
  },
};
let FETCH_RESULT = null, FETCH_THROWS = false, FETCHED = [];
const fetch = (url, opts) => {
  FETCHED.push({ url, opts });
  if (FETCH_THROWS) return Promise.reject(new TypeError("Failed to fetch"));
  return Promise.resolve(FETCH_RESULT);
};
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

const aiSrc = fs.readFileSync(process.argv[2], "utf8");
const a0 = aiSrc.indexOf("/* --- ask ai: config");
const a1 = aiSrc.indexOf("/* --- ask ai: ui");
if (a0 < 0 || a1 < 0) { console.error("ask-ai section not found"); process.exit(2); }
const AICONST = eval(aiSrc.slice(a0) + "\n({ AI_SYSTEM_PROMPT, AI_STORE_KEY, "
  + "AI_MAX_TOKENS, AI_HISTORY_MAX, AI_DIFF_LINES, AI_SRC_LINES, AI_CAPS, "
  + "AI_PROVIDERS });");

const failures = [];
let CHECKS = 0;
const check = (name, cond) => { CHECKS++; if (!cond) failures.push(name); };
const done = () => {
  if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
  // The count is asserted by the caller: "passed" with no checks would mean the
  // harness stopped before reaching them.
  console.log("frontend ask-ai checks passed (" + CHECKS + ")");
};

// A report sitting on one changed function of a two-function module, with the
// selected function's source mapped into a 60-line file.
const BEFORE = "define i32 @main() {\n  %1 = add i32 1, 1\n  ret i32 %1\n}";
const AFTER = "define i32 @main() {\n  ret i32 2\n}";
function useReport() {
  SUMMARY = {
    id: 7, lane: "ir", name: "InstCombinePass", passId: "InstCombinePass",
    runIndex: 31, timeMs: 1.2, changed: true, isCustom: false, isInput: false,
    lineDelta: { added: 2, removed: 1 }, spillCount: null, iselFns: null,
    analysisCounts: { run: 3, cached: 1, invalidated: 0 },
    functions: ["main", "square"], scope: "function",
  };
  CURRENT_MANIFEST = { metadata: {
    source: "/repo/sample.c", inputKind: "c", pipeline: "default<O2>",
    toolVersions: { opt: "Ubuntu LLVM version 22.1.0 (x86_64)" },
  }};
  CURRENT_PASS = { lane: "ir", log: "", functions: {
    main: { before: BEFORE, after: AFTER, changed: true, srcAfter: [null, [0, 4], [0, 5], null] },
    square: { before: "x", after: "x", changed: false },
  }};
  STATE.fn = "main";
  SOURCE_FILES = [{ path: "/repo/sample.c", name: "sample.c",
    text: Array.from({ length: 200 }, (_, i) => "line " + (i + 1)).join("\n") }];
}
"""

PROMPT_HARNESS = PRELUDE + r"""
// --- the system prompt: re-sent every turn, so it is the recurring cost ------
const SYS = buildSystemPrompt();
check("system prompt stays small enough to re-send every turn", SYS.length < 2600);
check("system prompt says what the report holds",
  /pass card/i.test(SYS) && SYS.includes("[module]") && /lane/i.test(SYS));
check("system prompt names the views it can be asked about",
  ["Diff", "CFG", "Source", "Structure", "ISel"].every(v => SYS.includes(v)));
check("system prompt asks for terse answers", /terse/i.test(SYS));
check("system prompt forbids restating the context", /restating/i.test(SYS));
check("system prompt points elsewhere instead of guessing",
  /instead of guessing/i.test(SYS));
check("system prompt tells the model what a cut section means",
  /truncated/.test(SYS) && /was cut/.test(SYS));
check("system prompt forbids inventing names and line numbers",
  /Never invent/i.test(SYS));
check("the prompt builder is stable, so the prefix stays cacheable",
  buildSystemPrompt() === SYS);

// --- what one question carries ----------------------------------------------
useReport();
const ctx = buildContext();
check("context names the source and the toolchain",
  ctx.includes("/repo/sample.c") && ctx.includes("Ubuntu LLVM version 22.1.0"));
check("context names the pass by number and name", ctx.includes("#031 InstCombinePass"));
check("context says which lane and scope the pass ran in",
  ctx.includes("IR (opt)") && ctx.includes("scope: function"));
check("context carries the line delta", ctx.includes("+2 -1"));
check("context lists which functions the pass changed",
  ctx.includes("1 changed of 2: main"));
check("context marks the selected function and its status",
  ctx.includes("selected function: main") && ctx.includes("changed by this pass"));
check("context carries a unified diff", ctx.includes("@@ -") && ctx.includes("\n+  ret i32 2"));
// The window opens at the first mapped line and runs for the source cap --
// a window, not the file, however long the file is.
const srcCap = AICONST.AI_CAPS.trimmed.srcLines;
check("context carries the mapped source window, with line numbers",
  ctx.includes(`--- source: sample.c lines 4-${3 + srcCap} of 200`)
  && ctx.includes("4: line 4"));
check("the source window stops at the cap", !ctx.includes("200: line 200"));
check("context has no credentials in it", !ctx.includes("api_key"));
check("context stays bounded", ctx.length < 12000);

// An unchanged function is still worth naming, but has no diff to send.
CURRENT_PASS.functions.main.changed = false;
SUMMARY.changed = false;
SUMMARY.lineDelta = null;
const quiet = buildContext();
check("an untouched function sends no diff", !quiet.includes("@@ -"));
check("an untouched function is labelled as untouched", quiet.includes("untouched by this pass"));

// --- the caps: an oversized diff is cut, and the user is asked first --------
useReport();
// Twice the line cap, so the fixture stays oversized whatever the cap is set
// to -- and big enough that the character cap is exceeded too, since a cap
// that only one of the two enforces is a cap nobody can reason about.
const N = AICONST.AI_CAPS.trimmed.diffLines * 2 + 200;
const many = Array.from({ length: N }, (_, i) => "  %" + i + " = add i32 %a, " + i);
CURRENT_PASS.functions.main.before = many.join("\n");
CURRENT_PASS.functions.main.after =
  many.map((l, i) => (i % 2 ? l.replace("add", "mul") : l)).join("\n");
CURRENT_PASS.functions.main.srcAfter = [];
const big = buildContext();
check("an oversized diff is cut", big.includes("… (truncated)"));
check("the cut diff respects the line cap",
  big.split("\n").filter(l => /^[ +\-@]/.test(l)).length <= AICONST.AI_DIFF_LINES + 1);
check("the tail of the function is not attached",
  !big.includes("%" + (N - 1) + " "));
check("even a huge function keeps the context bounded",
  big.length < AICONST.AI_CAPS.trimmed.diffChars + 8000);
check("truncate leaves a fitting string alone", truncate("a\nb", 100, 10) === "a\nb");
check("truncate marks a line cut", truncate("a\nb\nc", 100, 2).endsWith("… (truncated)"));
check("truncate marks a character cut",
  truncate("x".repeat(50), 10, 100).startsWith("x".repeat(10)));

// --- what was cut is reported, and the full context is offered --------------
// The answer that trailed off at "the diff truncates at block 54" is the bug
// this covers: a silently cut selection must be named before it is sent, and
// must be sendable in full on request.
const cut = contextDigest();
const cutDiff = cut.cuts[0] || {};
check("a cut selection reports what did not fit",
  cut.cuts.length === 1 && cutDiff.label.includes("main"));
check("the report names the size, not just 'truncated'",
  cutDiff.lines > AICONST.AI_CAPS.trimmed.diffLines &&
  cutDiff.chars > AICONST.AI_CAPS.trimmed.diffChars);
check("the cut is described in one line for the notice",
  /the diff of main is \d+ lines \(\d+ kB\)/.test(cutSummary(cut.cuts)));
check("a question is held back rather than sent cut",
  needsConsent(cut, undefined) === true);
check("answering the question sends it",
  needsConsent(cut, "trimmed") === false && needsConsent(cut, "full") === false);

// The whole point of "send the full context": the answer can reach the end.
const full = contextDigest({ full: true });
const tailMark = "%" + (N - 1) + " ";
check("the full context carries the tail the trimmed one dropped",
  full.text.includes(tailMark) && !cut.text.includes(tailMark));
check("the full context is uncut", full.cuts.length === 0);
check("the full context is still bounded",
  full.text.length <= AICONST.AI_CAPS.full.diffChars + 20000);
check("the full budget is bigger than the trimmed one, but not unbounded",
  AICONST.AI_CAPS.full.diffLines > AICONST.AI_CAPS.trimmed.diffLines &&
  AICONST.AI_CAPS.full.diffLines <= 20000);
// A cap nobody's question reaches is a cap that never teaches anything: the
// default budget has to clear an ordinary pass's whole-function diff (a few
// hundred lines), and the prompt only earns its place past that.
check("the default budget clears an ordinary whole-function diff",
  AICONST.AI_CAPS.trimmed.diffLines >= 400 &&
  AICONST.AI_CAPS.trimmed.diffChars >= 20000);
check("the two caps per section are in step, so neither dominates",
  Math.abs(AICONST.AI_CAPS.trimmed.diffChars / AICONST.AI_CAPS.trimmed.diffLines - 50) < 25);

// A selection that fits asks nothing.
useReport();
const small = contextDigest();
check("a question that fits is sent without a prompt",
  small.cuts.length === 0 && needsConsent(small, undefined) === false);
check("a big-but-fitting selection is not flagged",
  fitInfo("a\nb", 100, 10).over === false &&
  fitInfo("a\nb\nc", 100, 2).over === true &&
  fitInfo("x".repeat(50), 10, 100).over === true);

// --- history: context rides the newest turn only ----------------------------
useReport();
const history = [];
for (let i = 0; i < 12; i++) {
  history.push({ role: i % 2 ? "assistant" : "user", content: "turn " + i });
}
const messages = buildMessages(history, ctx, "why did it fold?");
check("history is trimmed to a fixed window",
  messages.length === AICONST.AI_HISTORY_MAX + 1);
check("replayed history is bare, with no context re-sent",
  messages.slice(0, -1).every(m => !m.content.includes("=== report ===")));
check("the newest turn carries the context and the question",
  messages[messages.length - 1].content.includes("=== report ===") &&
  messages[messages.length - 1].content.trimEnd().endsWith("why did it fold?"));
check("messages are only user/assistant turns",
  messages.every(m => m.role === "user" || m.role === "assistant"));

done();
"""

PROVIDER_HARNESS = PRELUDE + r"""
// --- config resolution ------------------------------------------------------
const anth = normalizeAIConfig({ provider: "anthropic", api_key: "sk-test" });
check("anthropic defaults its model and host",
  anth.model === "claude-opus-5" && anth.baseUrl === "https://api.anthropic.com");
check("the key is trimmed", normalizeAIConfig({ api_key: "  sk-x  " }).apiKey === "sk-x");
check("an unknown provider falls back to anthropic",
  normalizeAIConfig({ provider: "gemini", api_key: "k" }).provider === "anthropic");
check("openai-compatible defaults to openai's host",
  normalizeAIConfig({ provider: "openai-compatible" }).baseUrl === "https://api.openai.com/v1");
check("nothing configured means not configured",
  normalizeAIConfig(null) === null);

// --- anthropic --------------------------------------------------------------
const sys = buildSystemPrompt();
const req = buildRequest(anth, sys, [{ role: "user", content: "hi" }]);
check("anthropic posts to the messages endpoint",
  req.url === "https://api.anthropic.com/v1/messages");
check("the key travels in x-api-key, never in a bearer header",
  req.headers["x-api-key"] === "sk-test" && !req.headers.authorization);
check("anthropic-version is pinned", req.headers["anthropic-version"] === "2023-06-01");
check("the direct-browser-access header is sent (CORS is refused without it)",
  req.headers["anthropic-dangerous-direct-browser-access"] === "true");
check("the system prompt is a top-level field, not a message",
  req.body.system === sys && req.body.messages.every(m => m.role !== "system"));
// An output ceiling, not a target. It has to clear adaptive thinking, which
// current Claude models run by default and which is billed against this same
// budget -- too low a cap spends itself thinking and returns no text at all.
check("the reply is capped", req.body.max_tokens === AICONST.AI_MAX_TOKENS);
check("the cap leaves room for thinking plus an answer",
  AICONST.AI_MAX_TOKENS >= 8000 && AICONST.AI_MAX_TOKENS <= 64000);
// Thinking is on by default for current Claude models and the API default is
// what we want; sending the parameter would 400 on models that predate it.
check("no thinking parameter is sent, so any model name stays valid",
  !("thinking" in req.body));
check("the configured model is requested", req.body.model === "claude-opus-5");

// --- openai-compatible ------------------------------------------------------
const local = normalizeAIConfig({
  provider: "openai-compatible", api_key: "k2", model: "qwen3",
  base_url: "http://localhost:11434/v1/",
});
const oreq = buildRequest(local, sys, [{ role: "user", content: "hi" }]);
check("a trailing slash on the base url does not double up",
  oreq.url === "http://localhost:11434/v1/chat/completions");
check("a custom base url is honored, so ollama/openrouter work",
  buildRequest(normalizeAIConfig({ provider: "openai-compatible", api_key: "k",
    base_url: "https://openrouter.ai/api/v1" }), sys, [])
    .url === "https://openrouter.ai/api/v1/chat/completions");
check("openai-compatible authenticates with a bearer token",
  oreq.headers.authorization === "Bearer k2");
check("openai-compatible folds the system prompt into the messages",
  oreq.body.messages[0].role === "system" &&
  oreq.body.messages[0].content === sys && !("system" in oreq.body));
check("the caller's messages follow the system turn", oreq.body.messages[1].content === "hi");
check("openai-compatible sends no thinking parameter either",
  !("thinking" in oreq.body));

// --- response and error envelopes -------------------------------------------
check("anthropic text blocks are joined and non-text blocks skipped",
  extractText("anthropic", { content: [{ type: "text", text: "a" },
    { type: "thinking", thinking: "…" }, { type: "text", text: "b" }] }) === "ab");
check("openai-compatible choice content is read",
  extractText("openai-compatible", { choices: [{ message: { content: "yo" } }] }) === "yo");
check("an empty openai-compatible envelope is empty, not undefined",
  extractText("openai-compatible", {}) === "");
// A reasoning model answers in reasoning_content, and some servers fill only
// that -- which used to arrive as "the provider returned no text".
check("a reasoning-only reply is not read as an empty one",
  extractText("openai-compatible",
    { choices: [{ message: { content: "", reasoning_content: "thought" } }] }) === "thought");
check("content wins over the scratch work when both are present",
  extractText("openai-compatible",
    { choices: [{ message: { content: "answer", reasoning: "thought" } }] }) === "answer");
check("a part-array content is joined",
  extractText("openai-compatible",
    { choices: [{ message: { content: [{ text: "a" }, { text: "b" }] } }] }) === "ab");

// --- an endpoint that streams anyway ----------------------------------------
// The request asks for one JSON body, but some endpoints stream regardless;
// an unparsed body used to surface as an empty answer.
const sseOpenAI = [
  'data: {"choices":[{"delta":{"content":"folded "}}]}',
  'data: {"choices":[{"delta":{"content":"it"}}]}',
  "data: [DONE]",
].join("\n");
check("openai-compatible sse frames are joined",
  parseSSE(sseOpenAI) === "folded it");
check("anthropic sse frames are joined",
  parseSSE('event: content_block_delta\n'
    + 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}')
    === "hi");
check("unreadable frames are skipped, not fatal",
  parseSSE('data: {"choices":[{"delta":{"content":"a"}}]}\ndata: {oops\ndata: [DONE]') === "a");
check("a body with no frames parses to nothing", parseSSE("<html>") === "");
check("a keepalive comment is not mistaken for text",
  parseSSE(": ping\n\ndata: [DONE]") === "");

// --- why an answer came back empty ------------------------------------------
check("anthropic's stop reason and block types are reported",
  finishNote("anthropic", { stop_reason: "max_tokens",
    content: [{ type: "thinking" }] }) === "stop_reason: max_tokens; content: thinking");
check("openai-compatible's finish reason is reported",
  finishNote("openai-compatible", { choices: [{ finish_reason: "length",
    message: { content: "" } }] }) === "stop_reason: length; content: content");
check("an envelope with nothing in it says so",
  finishNote("anthropic", {}) === "content: empty");
check("the provider's own error message is surfaced",
  extractError("anthropic", 401, { error: { message: "invalid x-api-key" } }, "")
    === "401: invalid x-api-key");
check("a non-json error body is still shown",
  extractError("openai-compatible", 502, null, "<html>bad gateway</html>")
    .includes("bad gateway"));

// --- one full round trip, through the fetch stub -----------------------------
(async () => {
  FETCH_RESULT = { ok: true, status: 200,
    text: async () => JSON.stringify({ content: [{ type: "text", text: " folded it " }] }) };
  FETCHED = [];
  const answer = await askAI(anth, sys, [{ role: "user", content: "q" }]);
  check("the answer is trimmed", answer === "folded it");
  check("exactly one POST, carrying the built request",
    FETCHED.length === 1 && FETCHED[0].url === req.url &&
    FETCHED[0].opts.method === "POST" &&
    JSON.parse(FETCHED[0].opts.body).model === "claude-opus-5");

  FETCH_RESULT = { ok: false, status: 401,
    text: async () => JSON.stringify({ error: { message: "bad key" } }) };
  try {
    await askAI(anth, sys, []);
    check("a 401 rejects", false);
  } catch (err) {
    check("a 401 surfaces the provider's message", /401: bad key/.test(err.message));
  }

  FETCH_RESULT = { ok: true, status: 200, text: async () => JSON.stringify({ content: [] }) };
  try {
    await askAI(anth, sys, []);
    check("an empty reply rejects", false);
  } catch (err) {
    check("an empty reply says so",
      /returned no text/.test(err.message) && /content: empty/.test(err.message));
  }

  // The empty reply the user actually hit: adaptive thinking is on by default
  // for current Claude models and is billed against max_tokens, so a hard
  // question at a low cap can spend the whole budget and return no text block.
  FETCH_RESULT = { ok: true, status: 200, text: async () => JSON.stringify(
    { stop_reason: "max_tokens", content: [{ type: "thinking", thinking: "…" }] }) };
  try {
    await askAI(anth, sys, []);
    check("a thinking-only reply rejects", false);
  } catch (err) {
    check("a thinking-only reply names the budget and the block",
      /max_tokens/.test(err.message) && /content: thinking/.test(err.message));
    check("a budget-exhausted reply says the budget ran out",
      /output budget/.test(err.message));
  }

  FETCH_RESULT = { ok: true, status: 200, text: async () => JSON.stringify(
    { stop_reason: "refusal", content: [] }) };
  try {
    await askAI(anth, sys, []);
    check("a refusal rejects", false);
  } catch (err) {
    check("a refusal is reported as a refusal", /refused/.test(err.message));
  }

  // The bug the user hit: a streamed body was read as an empty answer.
  FETCH_RESULT = { ok: true, status: 200, text: async () => sseOpenAI };
  check("a streamed reply is not read as an empty one",
    (await askAI(anth, sys, [])) === "folded it");

  FETCH_RESULT = { ok: true, status: 200, text: async () => "<html>proxy</html>" };
  try {
    await askAI(anth, sys, []);
    check("an unreadable body rejects", false);
  } catch (err) {
    check("an unreadable body says the body was neither json nor sse",
      /neither JSON nor SSE/.test(err.message));
  }

  FETCH_THROWS = true;
  try {
    await askAI(anth, sys, []);
    check("a blocked request rejects", false);
  } catch (err) {
    check("a blocked request points at the base url and CORS",
      /base URL/.test(err.message) && /browser requests/.test(err.message));
  }
  FETCH_THROWS = false;

  done();
})().catch(err => { console.error(err); process.exit(3); });
"""

CONFIG_HARNESS = PRELUDE + r"""
(async () => {
  // --- a report built with --no-ai has no sidecar at all --------------------
  check("an unconfigured panel reports unconfigured",
    !isConfigured() && currentAIConfig() === null);
  FETCH_RESULT = { ok: false, status: 404 };
  const none = loadAIConfig();
  await settle();
  check("a missing sidecar falls through to the script wrapper", SCRIPTS.length === 1);
  SCRIPTS[0].onerror();  // ...which also is not there
  check("no sidecar and no storage resolves to nothing",
    (await none) === null && !isConfigured());
  SCRIPTS.length = 0;

  // --- the build-time sidecar configures the panel --------------------------
  FETCHED = [];
  FETCH_RESULT = { ok: true,
    json: async () => ({ provider: "anthropic", api_key: "sk-side", model: "claude-opus-5" }) };
  const built = await loadAIConfig();
  check("the sidecar configures the panel", isConfigured() && built.apiKey === "sk-side");
  check("the sidecar's missing base url falls back to the default",
    built.baseUrl === "https://api.anthropic.com");
  check("the fetched sidecar is injected by ai.js, not app.js's loader",
    FETCHED.length === 1 && FETCHED[0].url === "data/ai-config.json");
  // The bug behind "configure-ai --clear doesn't work": the build's key used to
  // be copied into localStorage, so it outlived both the config file and the
  // report, and the panel kept answering after the user had removed it.
  check("the build's key is not mirrored into localStorage", STORE.size === 0);
  renderAIKeyNote();
  check("the panel names the report's build as the source of the key",
    /this report's build/.test(aiEl("aiKeyNote").textContent));
  check("no differing browser key is invented", !/different key/.test(aiEl("aiKeyNote").textContent));
  check("forget is not offered for a key this browser never held",
    aiEl("aiForget").hidden === true);
  check("no caveat about a second copy that does not exist",
    aiEl("aiForgetNote").hidden === true);

  // --- file://: fetch is blocked, so the .js wrapper is injected ------------
  FETCH_THROWS = true;
  SCRIPTS.length = 0;
  const pending = loadAIConfig();
  await settle();
  check("a blocked fetch falls back to the sidecar script",
    SCRIPTS.length === 1 && SCRIPTS[0].src === "data/ai-config.js");
  window.__LLVM_LENS_AI_CONFIG__ = { provider: "openai-compatible", api_key: "sk-file",
    model: "qwen3", base_url: "http://localhost:11434/v1" };
  SCRIPTS[0].onload();
  const fromScript = await pending;
  check("the injected script configures the panel",
    fromScript.apiKey === "sk-file" && fromScript.provider === "openai-compatible");
  check("an explicitly empty base url keeps the one it was given",
    buildRequest(fromScript, "s", []).url === "http://localhost:11434/v1/chat/completions");
  FETCH_THROWS = false;

  // --- a key typed into the panel -------------------------------------------
  window.__LLVM_LENS_AI_CONFIG__ = null;
  FETCH_RESULT = { ok: false, status: 404 };
  SCRIPTS.length = 0;
  STORE.set(AICONST.AI_STORE_KEY, JSON.stringify({ provider: "anthropic",
    api_key: "sk-mine", model: "claude-opus-5", base_url: "" }));
  const stored = loadAIConfig();
  await settle();
  SCRIPTS[0].onerror();
  const mine = await stored;
  check("a stored key is used when the report carries none", mine.apiKey === "sk-mine");
  renderAIKeyNote();
  check("the panel attributes that key to this browser",
    /set in this browser/.test(aiEl("aiKeyNote").textContent));
  check("forget is offered for a browser copy", aiEl("aiForget").hidden === false);

  // --- both copies, disagreeing ---------------------------------------------
  FETCHED = [];
  FETCH_RESULT = { ok: true,
    json: async () => ({ provider: "anthropic", api_key: "sk-side", model: "claude-opus-5" }) };
  const both = await loadAIConfig();
  check("the report's build-time key outranks a stored one", both.apiKey === "sk-side");
  check("the sidecar is still consulted when a key is stored", FETCHED.length === 1);
  renderAIKeyNote();
  const note = aiEl("aiKeyNote").textContent;
  check("the panel names the build as the source of the key in force",
    /this report's build/.test(note));
  check("a key typed here that is being overridden is called out, not hidden",
    /different key/.test(note));
  check("the key is masked to its last four characters",
    note.includes("••••••••side") && !note.includes("sk-side"));
  check("the browser's copy is still forgettable", aiEl("aiForget").hidden === false);
  check("the panel warns that the build's copy outlives forgetting",
    aiEl("aiForgetNote").hidden === false);

  // --- one copy again: the caveat goes with the button it qualifies ---------
  STORE.clear();
  const soloBuilt = await loadAIConfig();
  check("the build's copy still answers on its own", soloBuilt.apiKey === "sk-side");
  renderAIKeyNote();
  check("the build-copy caveat is dropped once there is nothing to forget",
    aiEl("aiForgetNote").hidden === true && aiEl("aiForget").hidden === true);
  STORE.set(AICONST.AI_STORE_KEY, JSON.stringify({ provider: "anthropic",
    api_key: "sk-side", model: "claude-opus-5" }));

  // --- forget ---------------------------------------------------------------
  forgetAIKey();
  check("forget removes the browser's copy", STORE.size === 0);
  check("forget falls back to the build's key rather than to nothing",
    isConfigured() && currentAIConfig().apiKey === "sk-side");
  check("forget hides itself once there is nothing local left",
    aiEl("aiForget").hidden === true);
  check("the caveat goes with it, leaving only the build's copy",
    aiEl("aiForgetNote").hidden === true);

  // --- nothing left anywhere -------------------------------------------------
  FETCH_RESULT = { ok: false, status: 404 };
  SCRIPTS.length = 0;
  const gone = loadAIConfig();
  await settle();
  SCRIPTS[0].onerror();
  await gone;
  check("with no build copy and no stored one the panel is unconfigured",
    !isConfigured() && currentAIConfig() === null);
  renderAIKeyNote();
  check("the panel says the key is not set", aiEl("aiKeyNote").textContent === "not set");
  check("forget is not offered when there is nothing to forget",
    aiEl("aiForget").hidden === true);
  check("the build-copy warning is hidden when there is no build copy",
    aiEl("aiForgetNote").hidden === true);
  check("a broken stored value is ignored rather than thrown",
    (() => { STORE.set(AICONST.AI_STORE_KEY, "{not json"); return readStoredConfig() === null; })());
  STORE.clear();

  // --- rendering ------------------------------------------------------------
  const html = renderAnswerHtml("see <x>\n```\n  %1 = add i32 1, 2\n```\nand `%1`");
  check("a fenced block is rendered as an IR well",
    html.includes('class="ai-code"') && html.includes('class="tok-kw">add<'));
  check("prose outside a fence is escaped", html.includes("&lt;x&gt;"));
  check("inline code is marked", html.includes('class="ai-inline">%1<'));
  check("an unterminated fence still renders", typeof renderAnswerHtml("a\n```\nb") === "string");

  done();
})().catch(err => { console.error(err); process.exit(3); });
"""


# Pinned so a harness that stops early -- a thrown eval, a renamed helper --
# cannot pass by never reaching its checks. Bump when adding one.
PROMPT_CHECKS = 46
PROVIDER_CHECKS = 46
CONFIG_CHECKS = 39


def _run_node(harness: str, expect: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["node", "-e", harness, str(FRONTEND / "app.js"), str(FRONTEND / "ai.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert f"passed ({expect})" in result.stdout, result.stdout
    return result


def test_system_prompt_and_context_assembly():
    _run_node(PROMPT_HARNESS, PROMPT_CHECKS)


def test_provider_requests():
    _run_node(PROVIDER_HARNESS, PROVIDER_CHECKS)


def test_config_store_and_rendering():
    _run_node(CONFIG_HARNESS, CONFIG_CHECKS)
