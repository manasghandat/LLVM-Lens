"""Parse llc's ``-print-after-all`` machine-code dumps (backend lane).

Machine pass dumps in llc 22 look like:

    # *** IR Dump After X86 DAG->DAG Instruction Selection (x86-isel) ***:
    # Machine code for function main: IsSSA, TracksLiveness
    Function Live Ins: $edi in %5
    bb.0 (%ir-block.2):
      successors: %bb.1(0x50000000), %bb.3(0x30000000); %bb.1(62.50%), ...
      %0:gr32 = MOV32rm ...
    ...
    # End machine code for function main.

Conventions used here:
  * vregs are ``%<digits>`` (optionally ``:class`` or ``.subreg``);
  * physical registers are ``$name`` (``$rax``, ``$edi``, ``$noreg``);
  * stack slots are ``%stack.<n>`` / ``%fixed-stack.<n>``;
  * spills/reloads are memory-operand annotations ``:: (store ... into
    %stack.n)`` / ``:: (load ... from %stack.n)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Machine dump header; note the "# " prefix and trailing ":" vs IR headers.
MACHINE_HEADER_RE = re.compile(r"^# \*\*\* IR Dump After (.+?) \(([\w-]+)\) \*\*\*:$")
FUNC_START_RE = re.compile(r"^# Machine code for function (\S+): (.+)$")
FUNC_END_RE = re.compile(r"^# End machine code for function (\S+)\.$")
LIVE_INS_RE = re.compile(r"^Function Live Ins: (.+)$")
# Post-RA dumps prefix blocks with a byte size: "0B\tbb.0 (%ir-block.1):".
# Block names can contain dots: "bb.2.._crit_edge.loopexit".
BLOCK_RE = re.compile(r"^(?:\d+B\t)?bb\.([\w.$]+)(?: \(%ir-block\.([\w.$]+)\))?:$")
SUCCESSORS_RE = re.compile(r"^\s+successors: (.+)$")

# vregs: "%5", "%729:gr32", "%729.sub_32bit:gr64_with_sub_8bit", "%5.sub_32bit"
VREG_RE = re.compile(r"%(\d+)(?:\.sub_[a-z0-9_]+)?(?::[a-z0-9_]+)?")
PHYSREG_RE = re.compile(r"\$[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)?")
STACK_SLOT_RE = re.compile(r"%((?:fixed-)?stack)\.(\d+)")

SPILL_RE = re.compile(r":: \(store .* into %stack\.(\d+)\)")
RELOAD_RE = re.compile(r":: \(load .* from %stack\.(\d+)\)")


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
    spills: tuple[tuple[str, str], ...] = ()  # (kind, slot) kind in spill|reload
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
    spills: list[tuple[str, str]] = []
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
            successors = tuple(re.findall(r"%bb\.(\d+)", match.group(1)))
            continue
        if current_block is None or not line.strip():
            continue
        block_lines.append(line)
        # Instruction-level scanning.
        for spill_slot in SPILL_RE.findall(line):
            spills.append(("spill", spill_slot))
        for reload_slot in RELOAD_RE.findall(line):
            spills.append(("reload", reload_slot))
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
    """Best-effort vreg -> physreg/stack-slot map.

    Aligns instruction lines between the MIR state before VirtRegRewriter and
    after it, pairing ``%N`` operands with whatever the corresponding
    post-rewrite line replaced them with (a physical register or a stack slot).
    """
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
