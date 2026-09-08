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
