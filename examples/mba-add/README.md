# MBAAdd — an example custom pass plugin

`MBAAdd` rewrites every 8-bit integer `add` instruction into a mixed
boolean-arithmetic expression:

    a + b == (((a ^ b) + 2 * (a & b)) * 39 + 23) * 151 + 111

It is an LLVM new-pass-manager plugin (registers the pipeline name `mba-add`),
so it is exactly the kind of shared object LLVM-Lens's
`--load-pass-plugin` + `--custom-pass` flags are meant to consume.

## Build

```sh
make            # needs llvm-config-22 / llvm-config on PATH (LLVM 22)
```

This produces `libMBAAdd.so`.

## Run through LLVM-Lens

```sh
LLVM_LENS_BIN_DIR=/usr/lib/llvm-22/bin \
python -m cli.main examples/mba-add/demo.ll \
  -o /tmp/mba-report \
  --load-pass-plugin "$PWD/examples/mba-add/libMBAAdd.so" \
  --custom-pass mba-add
```

Then open `/tmp/mba-report/index.html`. The `MBAAdd` card appears in the IR lane
with a `custom` badge; its diff pane shows each 8-bit `add` replaced by the MBA
expression, and the CFG pane renders the rewritten functions.

Notes:

- `demo.ll` is plain LLVM IR because C sources promote 8-bit arithmetic to
  `i32`, which `MBAAdd` (correctly) leaves alone.
- The `.so` path must be absolute (or resolvable by `dlopen`) — a bare filename
  is not searched on the loader path.
- `--custom-pass` appends the pass as `function(<name>)`, so it runs after the
  default `default<O2>` module pipeline.
