"""Extract per-pass IR snapshots from opt's ``-print-changed=quiet`` output.

Dump format (opt, LLVM 22):

    *** IR Dump After <PassName> on <Function> ***
    <function IR ...>

Only functions a pass actually changed are printed (quiet mode), so a pass
absent from this list did not modify that function. Snapshots are per
(pass, function); pairing into before/after changes is diff.py's job.
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


@dataclass(frozen=True)
class IrSnapshot:
    pass_name: str
    function: str
    ir: str  # raw function/module text following the header

    @property
    def text(self) -> str:
        """Duck-typed for diff.pair_snapshots (MachineFunction uses .text)."""
        return self.ir


def parse_changed_ir(stderr: str) -> list[IrSnapshot]:
    """Parse a -print-changed=quiet stderr stream into ordered snapshots.

    Function-level dumps end at the closing ``}``: opt interleaves
    -debug-pass-manager output ("Running pass/analysis: …") right after it,
    and those lines must not become part of the function text (the CFG and
    diff views would show them as block content). Module dumps (``on
    [module]``) have no reliable terminator, so their body runs to the next
    header; log noise is filtered out in either case.
    """
    snapshots: list[IrSnapshot] = []
    pass_name: str | None = None
    function: str = ""
    body: list[str] = []

    def finish() -> None:
        nonlocal pass_name, function, body
        if pass_name is not None:
            snapshots.append(IrSnapshot(pass_name, function, strip_module_noise(body)))
        pass_name = None
        function = ""
        body = []

    for line in stderr.splitlines():
        match = HEADER_RE.match(line)
        if match:
            finish()
            pass_name = match.group(1)
            function = match.group(2)
            continue
        if pass_name is None or NOISE_RE.match(line):
            continue
        body.append(line)
        # Function-level dump: the closing brace ends the snapshot.
        if line.strip() == "}" and not function.startswith("["):
            finish()
    finish()
    return snapshots


def strip_module_noise(lines: list[str]) -> str:
    """Join a dump body, dropping the module preamble and metadata block.

    The blank runs the removals leave behind collapse to one and trailing
    blanks go, so a stripped module reads as one continuous listing rather
    than as gaps where the metadata used to be. Function-level dumps and
    Machine IR carry neither block and pass through unchanged.
    """
    kept: list[str] = []
    for line in lines:
        if MODULE_NOISE_RE.match(line):
            continue
        if not line.strip() and (not kept or not kept[-1].strip()):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)
