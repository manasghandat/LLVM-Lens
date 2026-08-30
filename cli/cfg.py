"""DOT generation for IR control-flow graphs and machine CFGs.

IR CFGs are parsed from function text (block labels + terminator successors);
machine CFGs come from the ``successors:`` lists of MIR blocks. DOT strings
are stored in the report JSON and rendered client-side.
"""

from __future__ import annotations

import re

from .parsers.mir import MachineFunction

# A block label at column 0, e.g. "entry:" / ".lr.ph:" / "!_Z3foov:".
LABEL_RE = re.compile(r"^([.\w\"$%-]+):\s*(;.*)?$")
# "label %x" tokens appear only in terminators (br/switch/indirectbr/callbr);
# switch labels may trail across continuation lines, so we collect them all
# per block and flush when the block ends (next label line).
BR_LABEL_RE = re.compile(r"label %([.\w\"$-]+)")

# Graph readability caps: node labels show the block name plus up to
# MAX_CODE_LINES instructions, each truncated to MAX_CODE_CHARS.
MAX_CODE_LINES = 6
MAX_CODE_CHARS = 52
# Drop debug metadata before truncation so the visible code is mostly
# operands: IR lines carry ", !dbg !36" tails; MIR lines carry
# "debug-location !19" tokens and "; file.c:line:col" source comments.
IR_DBG_TAIL_RE = re.compile(r",?\s+!dbg\s+![^\s,]+.*$")
MIR_TAIL_RE = re.compile(r"debug-location\s+!\d+\s*")


def _trim_line(line: str, dbg_tail_re: re.Pattern[str] | None = None) -> str:
    text = line if dbg_tail_re is None else dbg_tail_re.sub("", line)
    text = text.strip()
    if len(text) > MAX_CODE_CHARS:
        text = text[: MAX_CODE_CHARS - 1] + "…"
    return text


def _machine_instruction_line(line: str) -> str:
    """Clean one MIR instruction for display in a CFG node."""
    text = line.split(";", 1)[0]  # drop "; file.c:3:1" source comments
    return _trim_line(MIR_TAIL_RE.sub("", text))


def ir_cfg_dot(function_ir: str, function_name: str = "") -> str:
    """Build a DOT graph for one function's CFG from its IR text.

    Node labels carry the block name plus up to MAX_CODE_LINES instructions
    so the graph shows the actual code in each block.
    """
    nodes: list[tuple[str, list[str]]] = []
    edges: list[tuple[str, str]] = []
    current: str | None = None
    pending: list[str] = []
    code: list[str] = []

    def flush() -> None:
        nonlocal pending
        if current is not None:
            edges.extend((current, successor) for successor in pending)
        pending = []

    for line in function_ir.splitlines():
        if line.startswith(("define", "declare", "attributes")):
            continue
        label = LABEL_RE.match(line)
        if label:
            flush()  # block boundary: the previous block's terminators are done
            if current is not None:
                nodes.append((current, code))  # the finished block keeps its code
            current = label.group(1)
            code = []
            continue
        if current is None or not line.strip():
            continue
        pending.extend(BR_LABEL_RE.findall(line))
        text = line.strip()
        if text.startswith(";") or text.startswith("#dbg_"):
            continue  # comments and debug intrinsics stay out of the graph
        if len(code) < MAX_CODE_LINES:
            code.append(_trim_line(line, IR_DBG_TAIL_RE))
    flush()
    if current is not None:
        nodes.append((current, code))
    return _render_dot(nodes, edges)


def _resolve_successor(short: str, node_names: list[str]) -> str | None:
    """Map a short successor name ("bb.1") to its full block name.

    llc's ``successors:`` lines use the short form even when the block header
    is dotted ("bb.1..lr.ph.preheader"). Resolve by exact match, else unique
    prefix match.
    """
    if short in node_names:
        return short
    matches = [name for name in node_names if name.startswith(short + ".")]
    return matches[0] if len(matches) == 1 else None


def machine_cfg_dot(machine_function: MachineFunction) -> str:
    """Build a DOT graph for one function's machine CFG from MIR blocks.

    Node labels carry the block name plus up to MAX_CODE_LINES instructions.
    """
    nodes: list[tuple[str, list[str]]] = []
    edges: list[tuple[str, str]] = []
    for block in machine_function.blocks:
        code = [
            _machine_instruction_line(line)
            for line in block.lines
            if line.strip()
            and not line.strip().startswith((";", "DBG_VALUE"))
        ][:MAX_CODE_LINES]
        nodes.append((block.name, code))
    names = [name for name, _ in nodes]
    for block in machine_function.blocks:
        for successor in block.successors:
            target = _resolve_successor(successor, names)
            if target is not None:
                edges.append((block.name, target))
    return _render_dot(nodes, edges)


def _render_dot(nodes: list[tuple[str, list[str]]], edges: list[tuple[str, str]]) -> str:
    def escape(label: str) -> str:
        # Backslash and quote first, then newlines -> DOT's \n escape (which
        # the frontend parser turns back into real newlines).
        return '"' + label.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    parts = ["digraph {", '  rankdir="TB";']
    for i, (name, code) in enumerate(nodes):
        body = name if not code else name + "\n" + "\n".join("  " + line for line in code)
        parts.append(f"  n{i} [label={escape(body)}];")
    index = {name: i for i, (name, _) in enumerate(nodes)}
    for src, dst in edges:
        if src in index and dst in index:
            parts.append(f"  n{index[src]} -> n{index[dst]};")
    parts.append("}")
    return "\n".join(parts)
