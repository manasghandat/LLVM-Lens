# LLVM-Lens

Analyzes LLVM middle-end (`opt`) and backend (`llc`) pass pipelines on a source
file and generates a static, self-contained HTML report for exploring pass
behavior across IR and Machine IR.

## Features

- **Two lanes** — Lane A walks the `opt` new-pass-manager pipeline (LLVM IR),
  Lane B the `llc` backend (Machine IR).
- **Per-pass cards** — before/after diffs, control-flow graphs, per-function
  analysis activity, timing, spills, and register-allocation maps.
- **Source correlation** — every IR/MIR line maps back to its C/C++ source line
  when the input carries debug info.
- **Custom passes** — `--load-pass-plugin`, `--load`, and `--custom-pass` load
  plugins, force-dump them, and badge them in the report.
- **Ask AI** (optional) — a chat panel in the report that answers questions
  about the selected pass or function, backed by your own Anthropic or
  OpenAI-compatible key. Off until you configure one.
- **Fully static output** — the report is plain files; open `index.html` in any
  browser, no server needed.

## Requirements

- Python 3.10+
- LLVM 22 tools: `clang`, `opt`, `llc`, `llvm-dis`. The expected major version
  can be changed with `--llvm-version` or `LLVM_LENS_LLVM_MAJOR`; a specific
  install can be pointed at with `--bin-dir` or `LLVM_LENS_BIN_DIR`.

On Debian/Ubuntu:

```sh
apt install llvm-22 clang-22
```

## Install

```sh
pip install llvm-lens
```

Or, from a source checkout:

```sh
pip install -e .
```

## Usage

```sh
llvm-lens sample.c -o report
# or, without the console script:
python -m llvm_lens sample.c -o report
```

Then open `report/index.html`, or pass `--open` to launch it in your default
browser automatically:

```sh
llvm-lens sample.c --open
```

Run `llvm-lens --help` for every option.

### Examples

Run the default `default<O2>` pipeline on a C file:

```sh
llvm-lens sample.c --passes 'default<O2>' -o report
```

Load a pass plugin and badge it as custom:

```sh
llvm-lens demo.ll \
  --load-pass-plugin ./libMBAAdd.so \
  --custom-pass mba-add \
  -o report
```

Target a specific LLVM install:

```sh
LLVM_LENS_BIN_DIR=/usr/lib/llvm-22/bin llvm-lens sample.c
```

### Ask AI

The report's **ask AI** button (top right, beside `commands`) opens a panel that
answers questions about the pass and function you are looking at. It is
optional: without a key the panel says so and sends nothing.

Configure it once, and every report picks it up:

```sh
llvm-lens configure-ai            # prompts for provider, key, model
llvm-lens configure-ai --show     # what is stored (key masked)
llvm-lens configure-ai --clear
```

The key is written to `~/.llvm_lens_config` (mode `0600`) and copied into each
report's `data/ai-config.*`, which is gitignored. It never enters the report's
HTML or manifest, and no request leaves the browser except to the provider you
configured.

There are two places a key can live, and the panel's ⚙ always says which one it
is using:

- **the report you are reading**, from the build that produced it — this wins,
  so re-running `configure-ai` and rebuilding is enough to change the key;
- **this browser**, if you typed one into the panel, which applies to every
  report you open from then on.

`configure-ai --clear` removes the config file, but it cannot reach either of
those: a report directory built earlier keeps its own copy until you rebuild it
with `--no-ai`, and a key typed into the panel belongs to that browser. The
command says so when it runs, and the panel's `forget` button removes the
browser's copy.

Providers:

- `anthropic` — the Messages API. Default model `claude-opus-5`.
- `openai-compatible` — any `/chat/completions` endpoint, with `--base-url`
  for OpenRouter, Ollama, LM Studio, vLLM, or a proxy. OpenAI's own host
  refuses browser requests, so use one of those or a proxy in front of it.

A report directory is meant to be shared, so **rebuild with `--no-ai` before
handing it to anyone** — that leaves the credentials out entirely (and removes
any left by an earlier build into the same directory).

Only the context you are looking at is sent: the selected pass's summary, the
selected function's diff, and a bounded window of the mapped source. Whole
modules are never attached.

A pass that rewrites a large function produces a diff far bigger than an
ordinary question, so the panel asks before sending one of those rather than
quietly cutting it — the answer would otherwise trail off at the cut and read
as if that were the whole story. The prompt names what does not fit and offers
to send the selection whole, to send it trimmed (and marks the question so a
short answer is not mistaken for a complete one), or to cancel. Sending whole
is the default choice; it costs more, which is the trade the prompt is for.

## How it works

`llvm-lens` compiles the input to LLVM IR with `clang`, runs `opt` with
`-print-changed=quiet -debug-pass-manager -time-passes` (Lane A) and `llc` with
`-print-after-all -debug-pass=Structure -time-passes` (Lane B), parses the
captured streams, and emits `manifest.json` plus per-pass JSON chunks and a copy
of the report frontend. A crashed or timed-out `opt`/`llc` still yields a
partial report.

## Development

```sh
pip install -e '.[dev]'
pytest
```

## License

GPL-3.0 — see [LICENSE](LICENSE).
