# Examples

## Bundled samples

These ship inside the package, so they need no path: `--sample NAME` builds a
report for one. Each is a real shape of code rather than a benchmark, and each
was picked because it makes a different part of the report worth looking at.

- **`side-channel`** (the default) — a cache-timing side channel. `flush_cache`
  evicts a probe array, `getTime` reads one line of it with `rdtsc`, and `main`
  recovers a flag a byte at a time from whichever line came back fastest. The
  measurement loop and the flushed cache make it a picture of memory-heavy code
  that a straight-line benchmark would not give you.
- **`register-pressure`** — `mix16` keeps sixteen lanes of ChaCha/BLAKE2 state
  live across every round, more than x86-64's fifteen usable general-purpose
  registers, so the allocator spills and reloads. `mix4` does the same
  arithmetic on four lanes and spills nothing. Read it in `isel` and machine IR.
- **`vectorize`** — `scale_add` has no aliasing and no loop-carried dependency,
  so the loop vectorizer replaces it with wide loads and multiplies.
  `prefix_sum` carries each result into the next iteration and stays scalar.
  The contrast is in the before/after diff.
- **`switch-lowering`** — `region_of` is sixteen consecutive cases, lowered to a
  jump table; `kind_of` spreads the same count over a range of thousands and is
  lowered to a tree of comparisons instead. Same construct, two code shapes.
- **`licm`** — `weighted_sum` recomputes `scale * bias` on every iteration
  though neither value changes, so loop-invariant code motion lifts the multiply
  into the loop's preheader. `dependent_sum` looks the same but its factor is a
  function of `i`, so there is nothing to lift.

```sh
llvm-lens --sample --open                 # side-channel, the default
llvm-lens --sample switch-lowering --open
```

`llvm-lens --sample nope` lists the names. The sources are in
`src/llvm_lens/samples/`, which is the only copy — edit them there.

## mba-add

[`mba-add/`](mba-add/) is a worked example of a custom pass instead: a real
LLVM plugin, a `Makefile` to build it, and an `llvm-lens.yml` that wires the
plugin and its pass name up. It is not bundled, because its `.so` has to be
compiled against your own LLVM. See its [README](mba-add/README.md).

## json-query

[`json-query/`](json-query/) is a multi-file example: `jsonq`, a small JSON
tool in seven `.cpp` files over one shared header. Its `Makefile` builds the
program, and separately compiles each unit to IR and joins them with
`llvm-link`, so the report's IR lane starts with the whole program in front of
it — which a normal build, compiling and linking at the object level, never
gives the optimizer. Every unit carries debug info, so the source pane spans
all seven files. See its [README](json-query/README.md).

Note that it is a real C++ program: 99 functions, against the three of a
bundled sample. The report's IR lane prints the module after every pass that
changed it, so budget for the size — the README says how to scope a pipeline
down to the passes you actually want to read.
