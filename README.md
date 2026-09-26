# LLVM-Lens

Analyzes LLVM middle-end (`opt`) and backend (`llc`) pass pipelines on a source
file and generates a static, self-contained HTML report for exploring pass
behavior across IR and Machine IR.

## Features

- **Two lanes** — Lane A walks the `opt` new-pass-manager pipeline (LLVM IR),
  Lane B the `llc` backend (Machine IR).
- **Per-pass cards** — before/after diffs, control-flow graphs, per-function
  analysis activity, timing, spills, and register-allocation maps.
- **Per-line blame** — every line of IR or Machine IR knows which pass put it
  there. The Blame view shows that as a tinted gutter, and any line in the Diff
  or IR view can be clicked to see its whole chain — every pass that touched
  the line since the input, oldest first, each tagged created, rewritten or
  renamed. Clicking a pass in the chain jumps to its card.
- **Pass causality** (`--causality`) — which pass invocation made a later one
  able to do its work. The build re-runs `opt` once per invocation that
  changed the IR, with only that invocation skipped: a later invocation that
  stops changing the IR was *enabled* by it (loop-rotate → loop-simplify →
  indvars), one that starts was *pre-empted* by it (it did that pass's work
  first). The Causes view draws the whole graph beside Flow; the Causes tab
  shows one run's chain, enablers and effects, and whether the final IR
  differs without it.
- **Source correlation** — every IR/MIR line maps back to its C/C++ source line
  when the input carries debug info.
- **Custom passes** — `--load-pass-plugin`, `--load`, and `--custom-pass` load
  plugins, force-dump them, and badge them in the report.
- **Config file** — every flag, plus the per-tool extra arguments and the view
  the report opens on, in a `llvm-lens.yml` found by walking up from the
  working directory. See [Configuration](#configuration).
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
browser automatically.

If you have no C file to hand, the package ships five of its own — so you can see
a report without naming a path at all:

```sh
llvm-lens --sample --open                             # side-channel, the default
llvm-lens --sample register-pressure                  # 16 live lanes: spills and reloads
llvm-lens --sample vectorize  --passes 'default<O3>'  # a loop that widens, one that cannot, at O3
llvm-lens --sample switch-lowering                    # jump table vs. comparison tree
llvm-lens --sample licm                               # what the loop hoists, and what it cannot
```

[`examples/README.md`](examples/README.md) says what each one is for.

A few of the other flags, on a file of your own:

```sh
llvm-lens sample.c --passes 'mem2reg'          # Lane A is just that one pass
llvm-lens sample.c -o /tmp/lens                # somewhere other than ./report
llvm-lens demo.ll --target aarch64-linux-gnu   # another machine's Machine IR
llvm-lens demo.ll --load-pass-plugin ./libMBAAdd.so --custom-pass mba-add
llvm-lens sample.c --no-ai                     # drop the AI key before sharing
llvm-lens --sample vectorize --causality       # which pass enabled which
```

`--causality` builds `src/llvm_lens/plugins/ProvenanceTracker.cpp` with the
toolchain's `clang++` and `llvm-config` on first use (cached under
`~/.cache/llvm-lens`), then runs one `opt` per invocation that changed the IR,
in parallel; `--causality-limit` caps how many. The same analysis prints as
text with `python -m llvm_lens.causality input.ll --passes 'default<O2>'`.

`--passes` takes any new-pass-manager pipeline, `function(mem2reg,gvn)` included,
so you can decide how much of the pipeline the report covers.

Run `llvm-lens --help` for every option.

### Configuration

Anything you would otherwise repeat on the command line can live in a YAML
file. Write a fully commented starter file showing every key at its default:

```sh
llvm-lens --init-config            # writes ./llvm-lens.yml
llvm-lens --init-config ci/lens.yml
```

It never overwrites an existing file, so running it twice is safe. That file
lists every key at its default; [`llvm-lens.sample.yml`](llvm-lens.sample.yml)
in the repository root is the same list with a line of prose per key, and
[`examples/mba-add/llvm-lens.yml`](examples/mba-add/llvm-lens.yml) is a
filled-in one, configuring that example's plugin and custom pass — with it in
place, running `llvm-lens demo.ll` from that directory needs no flags at all.

Neither of those two is read on its own: only `llvm-lens.yml`, `llvm-lens.yaml`
and `.llvm-lens.yml` are discovered, so a sample can be committed beside the
code without changing how anything builds. Copy one, or name it explicitly
with `--config`.

`llvm-lens` looks for `llvm-lens.yml` (or `.yaml`, or `.llvm-lens.yml`) in the
working directory and then in each parent, so a file committed beside the code
it analyzes applies automatically. Failing that it reads
`$XDG_CONFIG_HOME/llvm-lens/config.yml` (`~/.config/llvm-lens/config.yml` by
default). Everything is optional — an unset key keeps the value shown in the
template.

```yaml
output: report
passes: 'default<O2>'
custom-passes: [mba-add]

llvm:
  bin-dir: /usr/lib/llvm-22/bin
  target: x86_64-unknown-linux-gnu

plugins:
  dir: build            # bare names below resolve against this
  pass: [libMBAAdd.so]  # -load-pass-plugin (opt + llc)
  legacy: []            # -load (llc backend only)

flags:                  # extra arguments for each tool
  clang: [-DVALUE=1]
  opt: []
  llc: ['-O3']

ui:                     # what the report opens on
  lane: ir              # ir | mir
  mode: cfg             # cfg | diff | ir | blame | src | isel | analyses | structure | pipeline
  analysis: [pdt]       # pdt | cdg | ddg | pdg | mdg | lnt | cg, or all
  orientation: side     # side | stack
  split-ratio: 0.5
  drawer: true
  drawer-tab: Log
  changed-only: false
  flow-both-lanes: true
```

`analysis` is a list, so the report can open on several graphs at once, stacked
in the pane. Clicking a name in the Graphs pane then focuses that one graph on
its own. `all` is shorthand for the six per-function graphs; the call graph
(`cg`) is module-wide, so it stays opt-in. Names are put back into the chip
row's order whatever order they are written in.

A flag always beats the file. The repeatable flags — `--load-pass-plugin`,
`--load`, `--custom-pass`, and `--clang-arg` / `--opt-arg` / `--llc-arg` —
*add* to what the file lists rather than replacing it, so a standing set of
plugins can live in the config and a one-off can be appended at the call site.
Those extra-argument flags need the `=` form for values starting with a dash:
`--llc-arg=-O3`.

Paths written in the config (`llvm.bin-dir`, `plugins.dir`, plugin entries) are
relative to the file itself, so a committed config works from any directory.
`output` is the exception — it stays relative to your shell.

The file is chosen before the build starts, and it is named in the report's
manifest, so a shared report says which settings produced it.

Use `--config FILE` to name one explicitly, or `--no-config` to ignore every
file and use the built-in defaults. A flag or file that cannot be used is
reported and the build stops:

```sh
$ llvm-lens sample.c
error: /home/me/proj/llvm-lens.yml: unknown setting 'passess'; did you mean passes?
(`llvm-lens --init-config` writes the full list)
```

The settings file is not the same thing as `~/.llvm_lens_config`, which stores
only the ask-AI credentials — see below.

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
LLVM_LENS_BIN_DIR=/usr/lib/llvm-22/bin llvm-lens --sample
```

### Use it as a library

The report is one way to ask about a pass. `llvm_lens.snapshot()` is the other:
it returns the IR, or the machine IR, immediately before or after a named pass,
for one file, as data.

```python
import llvm_lens

snap = llvm_lens.snapshot("sample.c", "greedy", when="after")
print(snap.format)              # "mir"
print(snap.functions["main"])   # main's machine code, exactly as llc printed it

# The IR lane runs opt instead, over a pipeline of your choosing.
snap = llvm_lens.snapshot("sample.c", "instcombine", when="before",
                          lane="ir", passes="default<O2>")
```

`when` is `"before"` or `"after"` — one value, so the two are mutually
exclusive by construction. `functions` maps each function name to its own text,
verbatim: nothing is re-rendered, so lines the report's structural model does
not track (a machine block's `successors:`, say) are still there. `text` is the
run's whole dump, `format` says whether that text is machine code or IR, and
`cmd` and `stderr_path` say how it was produced.

Name a pass the way LLVM does, by the id it prints in parentheses. That is often
not the obvious word — the register allocator is `greedy`, not `regalloc`, and
instruction selection is `x86-isel` on x86-64 — so ask what a file actually
runs:

```python
for pass_id, display_name in llvm_lens.list_machine_passes("sample.c"):
    print(pass_id, "—", display_name)
```

The file is optional. Called with no argument, `list_machine_passes()` compiles
a small scratch C file written under the system temp directory (`/tmp` on
Linux), so a caller with no source of their own can still ask what this target's
backend runs.

`snapshot()` returns llc's *print* output, which is a dump: readable, but not a
format any MIR consumer takes. `machine_ir()` returns the MIR serialization
format itself — the YAML documents LLVM's own MIR parser reads back, the first
holding the module's IR and each machine function following as its own:

```python
mir = llvm_lens.machine_ir("sample.c", stop_after="x86-isel")
mir.startswith("--- |")          # the embedded IR document
mir.count("\n---\n")             # then one document per machine function
```

Stop *after* a pass for the state it left, or *before* it for the state it saw —
exactly one of the two, since llc rejects the pair. `simplify=True` adds
`-simplify-mir` to drop debug metadata for a shorter text. Written to a file,
`llc -x mir` parses it and can run passes over it; a `snapshot()` dump cannot be
made to do either.

A pass can run more than once. `instcombine` runs 24 times over the bundled
sample under `default<O2>`, each run with its own before and after state, and a
machine pass that appears twice in llc's pipeline dumps every function twice.
Every invocation is kept: `snap.runs[i].text` and `.functions` are that
invocation's, while `snap.text` and `snap.functions` resolve to the first run
for `"before"` and the last for `"after"`. Pass `occurrence=` to pick another.

A name that matched no dump raises `SnapshotError` rather than returning an
empty answer. Both tools exit 0 in that case, having printed nothing, so the
silence would otherwise be indistinguishable from a pass that did nothing.

Captures are written to a fresh temporary directory, or to `out_dir=` if you
want to keep them; pass a `toolchain=` from `llvm_lens.toolchain` to skip
re-discovery when calling in a loop.

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

Because `opt` only dumps when a pass actually changed the IR, a pass that left
no dump changed nothing, and every line has a pass that put it there. The build
walks that whole stream to produce the per-line lineage, so a card is one pass
*run*, not one pass name: a pass that ran ten times and changed something ten
times gets ten cards. That is what lets the Diff view show one pass's work and
nothing else, and lets every name in a blame chain land on a card showing
exactly the state it names. `-time-passes` measures a pass over the whole lane
rather than any one run of it, so that number is stated once, on the pass's
first card.

## Development

```sh
pip install -e '.[dev]'
pytest
```

## License

GPL-3.0 — see [LICENSE](LICENSE).
