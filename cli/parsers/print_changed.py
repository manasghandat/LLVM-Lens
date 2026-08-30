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
            snapshots.append(IrSnapshot(pass_name, function, "\n".join(body)))
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
