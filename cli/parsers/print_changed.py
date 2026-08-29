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
    """Parse a -print-changed=quiet stderr stream into ordered snapshots."""
    snapshots: list[IrSnapshot] = []
    current: IrSnapshot | None = None
    body: list[str] = []

    for line in stderr.splitlines():
        match = HEADER_RE.match(line)
        if match:
            if current is not None:
                snapshots.append(IrSnapshot(
                    current.pass_name, current.function, "\n".join(body),
                ))
            current = IrSnapshot(match.group(1), match.group(2), "")
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        snapshots.append(IrSnapshot(current.pass_name, current.function, "\n".join(body)))
    return snapshots
