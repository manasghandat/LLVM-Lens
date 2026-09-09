"""Parse llc's -print-after-all machine-code dumps (backend lane)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Machine dump header; note the "# " prefix and trailing ":" vs IR headers.
MACHINE_HEADER_RE = re.compile(r"^# \*\*\* IR Dump After (.+?) \(([\w-]+)\) \*\*\*:$")
FUNC_START_RE = re.compile(r"^# Machine code for function (\S+): (.+)$")
FUNC_END_RE = re.compile(r"^# End machine code for function (\S+)\.$")
LIVE_INS_RE = re.compile(r"^Function Live Ins: (.+)$")
# Post-RA dumps prefix blocks with a byte size and optional alignment.
BLOCK_RE = re.compile(r"^(?:\d+B\t)?bb\.([\w.$]+)(?: \(%ir-block\.([\w.$]+)(?:, align \d+)?\))?:$")
SUCCESSORS_RE = re.compile(r"^\s+successors: (.+)$")

# vregs: "%5", "%729:gr32", "%729.sub_32bit:gr64_with_sub_8bit", "%5.sub_32bit"
VREG_RE = re.compile(r"%(\d+)(?:\.sub_[a-z0-9_]+)?(?::[a-z0-9_]+)?")
PHYSREG_RE = re.compile(r"\$[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?")
STACK_SLOT_RE = re.compile(r"%((?:fixed-)?stack)\.(\d+)")

SPILL_RE = re.compile(r":: \(store .* into %stack\.(\d+)\)")
RELOAD_RE = re.compile(r":: \(load .* from %stack\.(\d+)\)")


@dataclass(frozen=True)
class Spill:
    """One store to / load from a stack slot, with the site it happened at."""

    kind: str  # "spill" | "reload"
    slot: str  # stack slot number, as printed ("%stack.3" -> "3")
    block: str  # "bb.2"
    text: str  # the instruction itself, stripped


@dataclass(frozen=True)
class MachineBlock:
    name: str  # "bb.0"
    ir_block: str | None  # "2" for "%ir-block.2", None if unnamed
    successors: tuple[str, ...]  # "bb.1", "bb.3", ...
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class MachineFunction:
    name: str
    properties: str
    live_ins: str
    blocks: tuple[MachineBlock, ...] = ()
    spills: tuple[Spill, ...] = ()
    vregs: frozenset[str] = frozenset()
    physregs: frozenset[str] = frozenset()
    stack_slots: frozenset[str] = frozenset()

    @property
    def spill_count(self) -> int:
        return len(self.spills)

    @property
    def text(self) -> str:
        """Reconstruct the raw function text (used for before/after diffs)."""
        parts = [
            f"# Machine code for function {self.name}: {self.properties}",
            f"Function Live Ins: {self.live_ins}",
        ]
        for block in self.blocks:
            header = block.name
            if block.ir_block is not None:
                header += f" (%ir-block.{block.ir_block})"
            parts.append(header + ":")
            parts.extend(block.lines)
        return "\n".join(parts)


@dataclass(frozen=True)
class MirSnapshot:
    pass_name: str  # display name, e.g. "X86 DAG->DAG Instruction Selection"
    pass_id: str  # e.g. "x86-isel"
    functions: dict[str, MachineFunction] = field(default_factory=dict)
    line: int = 0  # 1-based line of the dump header in the stream


def _parse_function(lines: list[str]) -> MachineFunction:
    name, properties = FUNC_START_RE.match(lines[0]).groups()  # type: ignore[union-attr]
    live_ins = ""
    blocks: list[MachineBlock] = []
    spills: list[Spill] = []
    vregs: set[str] = set()
    physregs: set[str] = set()
    slots: set[str] = set()

    current_block: MachineBlock | None = None
    block_lines: list[str] = []
    block_name = ""
    block_ir = None
    successors: list[str] = []

    def flush_block() -> None:
        nonlocal current_block, block_lines, block_name, block_ir, successors
        if current_block is not None:
            blocks.append(MachineBlock(block_name, block_ir, tuple(successors), tuple(block_lines)))
        current_block, block_lines, successors = None, [], []

    for line in lines[1:]:
        match = LIVE_INS_RE.match(line)
        if match:
            live_ins = match.group(1)
            continue
        match = BLOCK_RE.match(line)
        if match:
            flush_block()
            block_name, block_ir = f"bb.{match.group(1)}", match.group(2)
            current_block = object()  # truthy marker
            continue
        match = SUCCESSORS_RE.match(line)
        if match:
            # Short block form; dedupe (hex list and probability tail repeat).
            names = (f"bb.{n}" for n in re.findall(r"%bb\.(\d+)", match.group(1)))
            successors = tuple(dict.fromkeys(names))
            continue
        if current_block is None or not line.strip():
            continue
        block_lines.append(line)
        # Instruction-level scanning.
        for spill_slot in SPILL_RE.findall(line):
            spills.append(Spill("spill", spill_slot, block_name, line.strip()))
        for reload_slot in RELOAD_RE.findall(line):
            spills.append(Spill("reload", reload_slot, block_name, line.strip()))
        vregs.update(VREG_RE.findall(line))
        physregs.update(PHYSREG_RE.findall(line))
        slots.update(STACK_SLOT_RE.findall(line))
    flush_block()

    return MachineFunction(
        name=name,
        properties=properties,
        live_ins=live_ins,
        blocks=tuple(blocks),
        spills=tuple(spills),
        vregs=frozenset(vregs),
        physregs=frozenset(physregs),
        stack_slots=frozenset(f"{kind}.{num}" for kind, num in slots),
    )


def parse_mir_snapshots(stderr: str) -> list[MirSnapshot]:
    """Parse an llc -print-after-all stderr stream into per-pass MIR snapshots."""
    snapshots: list[MirSnapshot] = []
    current: MirSnapshot | None = None
    functions: dict[str, MachineFunction] = {}
    func_lines: list[str] | None = None  # None = between functions

    def flush_function() -> None:
        nonlocal func_lines
        if func_lines is not None and current is not None:
            functions[func_lines[0].split(":", 1)[0].split()[-1]] = _parse_function(func_lines)
        func_lines = None

    for line_no, line in enumerate(stderr.splitlines(), start=1):
        match = MACHINE_HEADER_RE.match(line)
        if match:
            flush_function()
            if current is not None:
                snapshots.append(current)
            current = MirSnapshot(match.group(1), match.group(2), {}, line_no)
            functions = current.functions
            continue
        if current is None:
            continue
        match = FUNC_START_RE.match(line)
        if match:
            flush_function()
            func_lines = [line]
            continue
        match = FUNC_END_RE.match(line)
        if match:
            flush_function()
            continue
        if func_lines is not None:
            func_lines.append(line)
    flush_function()
    if current is not None:
        snapshots.append(current)
    return snapshots


def vreg_to_physreg(pre: MachineFunction, post: MachineFunction) -> dict[str, str]:
    """Best-effort vreg -> physreg/stack-slot map."""
    pre_lines = [line for block in pre.blocks for line in block.lines]
    post_lines = [line for block in post.blocks for line in block.lines]
    mapping: dict[str, str] = {}
    for pre_line, post_line in zip(pre_lines, post_lines):
        for vreg in VREG_RE.findall(pre_line):
            if vreg in mapping:
                continue
            replacements = []
            replacements += PHYSREG_RE.findall(post_line)
            replacements += [f"%{kind}.{num}" for kind, num in STACK_SLOT_RE.findall(post_line)]
            if replacements:
                mapping[vreg] = replacements[0]
    return mapping
