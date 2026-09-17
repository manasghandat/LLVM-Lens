# json-query — one program, seven translation units

`jsonq` parses a JSON document and evaluates dotted paths against it:

```sh
./build/jsonq                          # the document it carries, and some paths
./build/jsonq -f manifest.json hosts[0].addr service.replicas
./build/jsonq -f manifest.json ""      # the empty path is the whole document
```

It exists for the shape of its build, not for its features: seven `.cpp` files
over one shared header, linked into a single module before LLVM-Lens reads it.

## The files

| file | what it holds |
| --- | --- |
| `json.h` | the contract: the arena, the document model, the three stages |
| `arena.cpp` | bump allocation; the one place a document's memory is released |
| `lexer.cpp` | bytes to tokens, and decoding of string escapes |
| `parser.cpp` | recursive descent, building the document in the arena |
| `document.cpp` | `member()` and `kind_name()` — the read side of the model |
| `query.cpp` | path parsing, and evaluation of a path over the tree |
| `format.cpp` | writing a value back out as JSON |
| `main.cpp` | the driver, and the document it queries when given no file |

Nothing here reaches for a standard container. The arena is the only
allocator, so a document and everything read out of it are one region; that
keeps the module small enough to read in a report, and it is a real design for
a parser of this size rather than a concession.

## Build

```sh
make          # build/jsonq and build/json-query.ll
make run      # the binary, on its built-in document
make report   # the report, opened in a browser
make clean
```

Two different compilations, on purpose:

- `build/jsonq` is the program, compiled at `-O2` the way you would build it.
- `build/json-query.ll` is the module the report reads. Each unit is compiled
  at `-O0` with `-Xclang -disable-O0-optnone` and `-g` — the exact flags
  LLVM-Lens passes to clang itself — and the seven results are joined with
  `llvm-link`. So the IR lane starts where LLVM-Lens's own compile step would
  have started, with the whole program in front of it.

`llvm-link` writes bitcode whatever the output file is called, so the Makefile
links to `.bc` and disassembles with `llvm-dis`; the report reads text.

## The report

```sh
llvm-lens build/json-query.ll --open
```

That is `make report`. What is worth looking at:

- **The whole program at once.** Because the units are joined at the IR level
  rather than the object level, `opt` sees all seven at once. This is the thing
  a normal `-O2` build does not do — it optimizes each unit alone and never
  inlines across a `call` into another file.
- **The source pane spans seven files.** Every unit was compiled with `-g`, so
  selecting a function shows the `.cpp` it came from beside its IR. The debug
  paths are absolute (`$(CURDIR)` in the Makefile), so this works whatever
  directory the report is built from.
- **Six switches, six jump tables.** `Lexer::next`, `Parser::parse_value`,
  `unescape`, `print`, `print_string` and `kind_name` each switch on a small
  integer, and every one is lowered to an indirect branch. The MIR lane shows
  the table appear at `isel`, and the same functions are readable in the source
  pane one pane over.

## What the report costs

Both lanes print their whole module after every pass, even for a pass that
touched one function. Cost therefore scales with passes x functions, and this
program has 99 functions where the shipped C samples have three. Measured on
this machine:

| | time | report on disk |
| --- | --- | --- |
| `llvm-lens build/json-query.ll` | minutes | ~400 MB |
| `--passes 'module(inline,globaldce)'` | ~24 s | ~400 MB |
| a bundled sample (3 functions) | ~1.5 s | ~9 MB |

Scoping `--passes` cuts the IR lane from ~776 module dumps to 3, which is the
difference between minutes and seconds for *building* the report -- but the
machine lane is not affected: `llc` runs its full pipeline either way, and its
86 passes over a module this size are most of the 400 MB. Only a smaller
program makes that smaller.

None of this is specific to this example; it is what LLVM-Lens does with any
program of this size, and it is worth knowing before you point it at your own
code. `--passes` takes any new-pass-manager pipeline, so you can decide how
much of the IR lane is worth reading.
