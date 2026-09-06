"""Extract per-pass IR snapshots from opt's ``-print-changed=quiet`` output.

Dump format (opt, LLVM 22):

    *** IR Dump After <PassName> on <Function> ***
    ; ModuleID = 'sample.ll'
    <whole module ...>

Only entities a pass actually changed are printed (quiet mode), so a pass
absent from this list did not modify that function. The header still names the
entity the pass ran on -- a function, ``[module]``, an SCC, a loop -- but the
body is the whole module, because the runner passes ``-print-module-scope``:
a bare function dump references ``!dbg !N`` without defining it, and opt
renumbers metadata as passes drop it, so a function-scope capture cannot be
correlated with source without a second, module-scope run of the same
pipeline. Printing at module scope once is cheaper than running opt twice, and
it cannot drift from the run it describes. Carving the header's entity back
out of the module is main.py's job (``_entity_text``); pairing snapshots into
before/after changes is diff.py's.

Bodies from before that change -- an older report, a hand-written fixture --
still parse: a dump that does not open on the module preamble is read as the
bare function it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "*** IR Dump After SimplifyCFGPass on main ***"  ("on [module]" for module passes)
HEADER_RE = re.compile(r"^\*\*\* IR Dump After (.+?) on (.+) \*\*\*$")
# -debug-pass-manager log lines interleave with the dumps on stderr; they
# are never IR, so they stay out of every snapshot body. Loop-pass dumps
# print a bare loop fragment (no closing brace), so their bodies run to the
# next header and rely on this filter.
NOISE_RE = re.compile(r"^(?:Running (?:pass|analysis)|Invalidating analysis):")
# A module dump opens with a preamble (ModuleID / source_filename / the two
# target lines) and closes with the metadata block: the named nodes
# (!llvm.dbg.cu, !llvm.module.flags, !llvm.ident) plus the numbered
# `!N = !DI...` debug graph. Under -g that graph is over half the dump, it
# renumbers whenever a pass drops a node -- so it churns the diff without a
# single instruction changing -- and none of it is code. It is dropped here,
# at the one point every consumer reads through: the diff, the CFGs, the
# source mapping and the emitted report all see the same stripped text.
#
# Column 0 only. Inside a function nothing starts with `!`, so an
# instruction's own `!dbg !36` reference is mid-line and survives untouched;
# `attributes #N = { ... }` is not debug info and stays as well.
MODULE_NOISE_RE = re.compile(
    r"^(?:; ModuleID = |source_filename = |target (?:datalayout|triple) = |!)"
)

# Whatever entity the header names, a -print-module-scope body opens on the
# module preamble. That is what tells a whole module apart from a bare function
# dump, and only the latter ends at a closing brace.
MODULE_PREAMBLE_RE = re.compile(r"^; ModuleID = ")

# "define dso_local i64 @getTime(ptr noundef %0) #0 !dbg !49 {" -- the name is
# the first `@thing(` on the line; quoted names ("foo bar") are legal too.
DEFINE_RE = re.compile(r'^define\b[^@]*@("(?:[^"\\]|\\.)*"|[\w.$\-]+)\s*\(')


@dataclass(frozen=True)
class IrSnapshot:
    pass_name: str
    function: str
    ir: str  # dump body, module bookkeeping removed
    # The bookkeeping that was removed, kept only for its `!N = !DI...` nodes:
    # they are the table that maps this snapshot's `!dbg !N` back to source.
    # Empty for a dump that carried none (no -g, or a bare function dump).
    metadata: str = ""

    @property
    def text(self) -> str:
        """Duck-typed for diff.pair_snapshots (MachineFunction uses .text)."""
        return self.ir


def parse_changed_ir(stderr: str) -> list[IrSnapshot]:
    """Parse a -print-changed=quiet stderr stream into ordered snapshots.

    A whole module has no reliable terminator -- and under
    ``-print-module-scope`` every dump is one -- so a body runs to the next
    header. A bare function dump instead ends at its closing ``}``: opt
    interleaves -debug-pass-manager output ("Running pass/analysis: …") right
    after it, and those lines must not become part of the function text (the
    CFG and diff views would show them as block content). The two are told
    apart by the body itself, not by the header, because a module-scope dump
    is headed by whichever entity the pass ran on. Log noise is filtered out
    either way.
    """
    snapshots: list[IrSnapshot] = []
    pass_name: str | None = None
    function: str = ""
    body: list[str] = []
    module_scope: bool = False

    def finish() -> None:
        nonlocal pass_name, function, body, module_scope
        if pass_name is not None:
            code, metadata = split_module_noise(body)
            snapshots.append(IrSnapshot(pass_name, function, code, metadata))
        pass_name = None
        function = ""
        body = []
        module_scope = False

    for line in stderr.splitlines():
        match = HEADER_RE.match(line)
        if match:
            finish()
            pass_name = match.group(1)
            function = match.group(2)
            continue
        if pass_name is None or NOISE_RE.match(line):
            continue
        if not body and MODULE_PREAMBLE_RE.match(line):
            module_scope = True
        body.append(line)
        # Bare function dump: the closing brace ends the snapshot.
        if line.strip() == "}" and not module_scope and not function.startswith("["):
            finish()
    finish()
    return snapshots


def split_module_noise(lines: list[str]) -> tuple[str, str]:
    """Separate a dump body into (code, module bookkeeping).

    The bookkeeping half never reaches the report, but it is not thrown away
    at the point of removal: its ``!N = !DI...`` nodes are exactly the table
    that resolves the code half's ``!dbg !N`` references back to source lines,
    and this is the last place the two are still known to belong together.

    In the code half, the blank runs the removals leave behind collapse to one
    and trailing blanks go, so a stripped module reads as one continuous
    listing rather than as gaps where the metadata used to be. Bare function
    dumps and Machine IR carry neither block and pass through unchanged, with
    nothing in the bookkeeping half.
    """
    kept: list[str] = []
    noise: list[str] = []
    for line in lines:
        if MODULE_NOISE_RE.match(line):
            noise.append(line)
            continue
        if not line.strip() and (not kept or not kept[-1].strip()):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept), "\n".join(noise)


def strip_module_noise(lines: list[str]) -> str:
    """The code half of :func:`split_module_noise`, for callers with no use
    for the metadata (the input cards, which read their table off the module
    they were handed)."""
    return split_module_noise(lines)[0]


def split_module_functions(module_text: str) -> dict[str, str]:
    """Split a module dump into {function name: its text}.

    The bodies come out byte-identical to what a function-scope dump of the
    same state prints -- same printer, same slot numbering -- so they can be
    used as the "before" of a later function dump. The ``; Function Attrs:``
    comment above a define is part of the function dump, so it is kept here
    too, or the pairing would show it as an added line.

    Only definitions are returned; declarations, globals and the rest of the
    module are not something a function dump ever shows.
    """
    lines = module_text.splitlines()
    functions: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = DEFINE_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        start = index
        while start > 0 and lines[start - 1].startswith(";"):
            start -= 1
        end = index
        while end < len(lines) and lines[end] != "}":
            end += 1
        functions[match.group(1).strip('"')] = "\n".join(lines[start : end + 1])
        index = end + 1
    return functions
