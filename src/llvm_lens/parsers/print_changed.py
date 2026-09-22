"""Extract per-pass IR snapshots from opt's -print-changed=quiet output."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# "*** IR Dump After SimplifyCFGPass on main ***"  ("on [module]" for module passes)
HEADER_RE = re.compile(r"^\*\*\* IR Dump After (.+?) on (.+) \*\*\*$")
# The same dump for a pass named outright (-print-before/-print-after) rather
# than reached through -print-changed: commented out, and it can read "Before".
DIRECT_HEADER_RE = re.compile(r"^(?:; )?\*\*\* IR Dump (Before|After) (.+?) on (.+) \*\*\*$")
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
    line: int = 0  # 1-based line of the dump's header, to place it in the stream
    when: str = "after"  # "before" | "after"
    # The dump verbatim, header line included. `ir` is the same text with the
    # module preamble and metadata stripped, which is what the report renders.
    raw: str = ""

    @property
    def text(self) -> str:
        """Duck-typed for diff.pair_snapshots (MachineFunction uses .text)."""
        return self.ir


def _changed_header(line: str) -> tuple[str, str, str] | None:
    """-print-changed always dumps after the pass that changed something."""
    match = HEADER_RE.match(line)
    return None if match is None else ("after", match.group(1), match.group(2))


def _direct_header(line: str) -> tuple[str, str, str] | None:
    match = DIRECT_HEADER_RE.match(line)
    if match is None:
        return None
    return match.group(1).lower(), match.group(2), match.group(3)


def _walk_dumps(
    stderr: str,
    header: Callable[[str], tuple[str, str, str] | None],
) -> list[IrSnapshot]:
    """Group an opt dump stream into ordered snapshots.

    *header* reads a line as (when, pass name, function) or None — the only
    thing the two dump forms disagree about.
    """
    snapshots: list[IrSnapshot] = []
    pass_name: str | None = None
    function: str = ""
    when: str = "after"
    body: list[str] = []
    module_scope: bool = False
    header_line = 0
    header_text = ""

    def finish() -> None:
        nonlocal pass_name, function, when, body, module_scope, header_line, header_text
        if pass_name is not None:
            code, metadata = split_module_noise(body)
            raw = "\n".join([header_text, *body]) if header_text else "\n".join(body)
            snapshots.append(
                IrSnapshot(pass_name, function, code, metadata, module_scope,
                           header_line, when, raw)
            )
        pass_name = None
        function = ""
        when = "after"
        body = []
        module_scope = False
        header_line = 0
        header_text = ""

    for line_no, line in enumerate(stderr.splitlines(), start=1):
        read = header(line)
        if read is not None:
            finish()
            when, pass_name, function = read
            header_line = line_no
            header_text = line
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


def parse_changed_ir(stderr: str) -> list[IrSnapshot]:
    """Parse a -print-changed=quiet stderr stream into ordered snapshots."""
    return _walk_dumps(stderr, _changed_header)


def parse_direct_ir(stderr: str) -> list[IrSnapshot]:
    """Parse opt's explicit -print-before/-print-after dumps.

    These carry the direction they were asked for, and appear for the named pass
    whether or not it changed anything.
    """
    return _walk_dumps(stderr, _direct_header)


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
