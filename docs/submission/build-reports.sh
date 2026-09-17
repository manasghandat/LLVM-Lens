#!/usr/bin/env bash
# Rebuilds every report the submission's screenshots were taken from.
# Set S to somewhere with ~1 GB free; nothing here is committed.
set -euo pipefail

S="${S:-/tmp/lens-shots}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

rm -rf "$S"
mkdir -p "$S"

# The real program: json-query, one translation unit.
llvm-lens examples/json-query/lexer.cpp -o "$S/jsonq" --passes 'default<O2>' --no-ai

# The same program as one linked module, all seven translation units.
( cd examples/json-query && make )
llvm-lens examples/json-query/build/json-query.ll -o "$S/jsonq-linked" --passes 'default<O2>' --no-ai

# The custom pass: build the plugin, then let the committed llvm-lens.yml wire it.
( cd examples/mba-add && make clean && make )
( cd examples/mba-add && llvm-lens demo.ll -o "$S/mba" --no-ai )

# Purpose-built samples, each written to make one behaviour legible.
llvm-lens --sample register-pressure -o "$S/regpressure" --no-ai
llvm-lens --sample vectorize --passes 'default<O3>' -o "$S/vectorize" --no-ai
llvm-lens --sample switch-lowering -o "$S/switch" --no-ai
llvm-lens --sample licm -o "$S/licm" --no-ai

echo
echo "built into $S:"
du -sh "$S"/*
