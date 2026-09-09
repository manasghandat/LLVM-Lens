"""Correlate the LLVM IR llc was handed against the machine IR ISel produced."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .cfg import LABEL_RE, _implicit_entry_name
from .parsers.mir import BLOCK_RE

# "bb.3..lr.ph" -> ".lr.ph"; a bare "bb.3" has no IR block to name.
MIR_BLOCK_NAME_RE = re.compile(r"^bb\.\d+\.(.+)$")
# The IR value behind a machine memory operand: ":: (load ... from %ir.4)".
IR_VALUE_REF_RE = re.compile(r"%ir\.([\w.$-]+)")
# An IR instruction defining a value: "  %7 = add i64 %5, %6".
IR_DEF_RE = re.compile(r"^\s+%([\w.$-]+) = ")
# A named or unnamed value in a define's parameter list.
PARAM_RE = re.compile(r"%([\w.$-]+)\b")


@dataclass(frozen=True)
class BlockSpan:
    name: str
    start: int  # 0-based, the label line (or the first instruction, for an entry)
    end: int  # 0-based, inclusive


def ir_block_spans(function_ir: str) -> list[BlockSpan]:
    lines = function_ir.splitlines()
    spans: list[BlockSpan] = []
    define_line: str | None = None
    current: str | None = None
    start = 0
    end = len(lines) - 1

    for index, line in enumerate(lines):
        if define_line is None:
            if line.startswith("define"):
                define_line = line
            continue
        if line.strip() == "}":
            end = index - 1
            break
        label = LABEL_RE.match(line)
        if label:
            if current is not None:
                spans.append(BlockSpan(current, start, index - 1))
            current, start = label.group(1), index
            continue
        if current is None and line.strip():
            current, start = _implicit_entry_name(define_line), index

    if current is not None:
        spans.append(BlockSpan(current, start, max(start, end)))
    return spans


def mir_block_spans(mir_text: str) -> list[tuple[BlockSpan, str | None]]:
    lines = mir_text.splitlines()
    spans: list[tuple[BlockSpan, str | None]] = []
    current: tuple[str, str | None] | None = None
    start = 0

    for index, line in enumerate(lines):
        match = BLOCK_RE.match(line)
        if not match:
            continue
        if current is not None:
            spans.append((BlockSpan(current[0], start, index - 1), current[1]))
        current = (f"bb.{match.group(1)}", match.group(2))
        start = index

    if current is not None:
        spans.append((BlockSpan(current[0], start, len(lines) - 1), current[1]))
    return [(span, _ir_block_of(span.name, ir_block)) for span, ir_block in spans]


def _ir_block_of(name: str, ir_block: str | None) -> str | None:
    if ir_block:  # "bb.0 (%ir-block.2)": the IR block is unnamed, this is its slot
        return ir_block
    match = MIR_BLOCK_NAME_RE.match(name)
    return match.group(1) if match else None


def _ir_definitions(function_ir: str) -> dict[str, int]:
    lines = function_ir.splitlines()
    defs: dict[str, int] = {}
    for index, line in enumerate(lines):
        if line.startswith("define"):
            for name in PARAM_RE.findall(line[line.find("(") + 1:]):
                defs.setdefault(name, index)
            continue
        match = IR_DEF_RE.match(line)
        if match:
            defs.setdefault(match.group(1), index)
    return defs


def correlate(function_ir: str, mir_text: str) -> dict[str, object] | None:
    ir_spans = ir_block_spans(function_ir)
    mir_spans = mir_block_spans(mir_text)
    if not ir_spans or not mir_spans:
        return None

    index_of = {span.name: i for i, span in enumerate(ir_spans)}
    mir_blocks = [
        {
            "name": span.name,
            "start": span.start,
            "end": span.end,
            # Index into irBlocks, or None for a block the backend invented.
            "irBlock": index_of.get(ir_name) if ir_name else None,
        }
        for span, ir_name in mir_spans
    ]

    defs = _ir_definitions(function_ir)
    refs: dict[str, list[int]] = {}
    for index, line in enumerate(mir_text.splitlines()):
        targets = sorted({defs[name] for name in IR_VALUE_REF_RE.findall(line) if name in defs})
        if targets:
            refs[str(index)] = targets

    return {
        "ir": function_ir,
        "irBlocks": [{"name": s.name, "start": s.start, "end": s.end} for s in ir_spans],
        "mirBlocks": mir_blocks,
        "refs": refs,
    }
