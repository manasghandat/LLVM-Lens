"""Extract per-pass IR snapshots from opt's -print-changed=quiet output."""

from __future__ import annotations

import re
from dataclasses import dataclass

# "*** IR Dump After SimplifyCFGPass on main ***"  ("on [module]" for module passes)
HEADER_RE = re.compile(r"^\*\*\* IR Dump After (.+?) on (.+) \*\*\*$")
# -debug-pass-manager log lines interleave with the dumps; never IR.
NOISE_RE = re.compile(r"^(?:Running (?:pass|analysis)|Invalidating analysis):")
# Module preamble + metadata block; dropped so every consumer sees the same text.
MODULE_NOISE_RE = re.compile(
    r"^(?:; ModuleID = |source_filename = |target (?:datalayout|triple) = |!)"
)

# A -print-module-scope body opens on the module preamble.
MODULE_PREAMBLE_RE = re.compile(r"^; ModuleID = ")

# The name is the first `@thing(` on the define line (quoted names allowed).
DEFINE_RE = re.compile(r'^define\b[^@]*@("(?:[^"\\]|\\.)*"|[\w.$\-]+)\s*\(')


@dataclass(frozen=True)
class IrSnapshot:
    pass_name: str
    function: str
    ir: str  # dump body, module bookkeeping removed
    # Removed bookkeeping, kept for its `!N = !DI...` source-mapping nodes.
    metadata: str = ""
    module_scope: bool = False

    @property
    def text(self) -> str:
        """Duck-typed for diff.pair_snapshots (MachineFunction uses .text)."""
        return self.ir


def parse_changed_ir(stderr: str) -> list[IrSnapshot]:
    """Parse a -print-changed=quiet stderr stream into ordered snapshots."""
    snapshots: list[IrSnapshot] = []
    pass_name: str | None = None
    function: str = ""
    body: list[str] = []
    module_scope: bool = False

    def finish() -> None:
        nonlocal pass_name, function, body, module_scope
        if pass_name is not None:
            code, metadata = split_module_noise(body)
            snapshots.append(IrSnapshot(pass_name, function, code, metadata, module_scope))
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
    """Separate a dump body into (code, module bookkeeping)."""
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
    """The code half of :func:`split_module_noise`, dropping the metadata."""
    return split_module_noise(lines)[0]


def split_module_functions(module_text: str) -> dict[str, str]:
    """Split a module dump into {function name: its text}."""
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
