# LLVM-Lens

A web frontend over the LLVM tools: submit textual LLVM IR, run a pass
pipeline, inspect the result.

## Requirements

- Go 1.24 or newer
- LLVM tools on `PATH` (`opt` at minimum)

## Layout

```
cmd/llvm-lens/      server entry point: flags, wiring
internal/config/    configuration, loaded from .env
internal/llvm/      LLVM tool discovery, invocation, pass registry
internal/server/    HTTP routing and handlers
web/                frontend, to be embedded via go:embed
  static/           index.html, app.js, style.css
testdata/           sample IR modules
```

## Configuration

Runtime configuration is read from a `.env` file in the working directory.
Exported shell variables take precedence over the file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLVM_LENS_ADDR` | `:8080` | Listen address |
| `LLVM_LENS_BIN_DIR` | *(PATH)* | Directory holding the LLVM tools |
| `LLVM_LENS_RUN_TIMEOUT` | `30s` | Per-invocation timeout |
| `LLVM_LENS_MAX_SOURCE_BYTES` | `4194304` | Max accepted IR module size |

## Development

```sh
make fmt vet test
make build           # bin/llvm-lens
```

## License

GPL-3.0 — see [LICENSE](LICENSE).
