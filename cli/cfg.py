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


def ir_cfg_dot(function_ir: str, function_name: str = "") -> str:
    """Build a DOT graph for one function's CFG from its IR text."""
    nodes: list[str] = []
    edges: list[tuple[str, str]] = []
    current: str | None = None
    pending: list[str] = []

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
            current = label.group(1)
            nodes.append(current)
            continue
        if current is None or not line.strip():
            continue
        pending.extend(BR_LABEL_RE.findall(line))
    flush()
    return _render_dot(nodes, edges)


def machine_cfg_dot(machine_function: MachineFunction) -> str:
    """Build a DOT graph for one function's machine CFG from MIR blocks."""
    nodes: list[str] = []
    edges: list[tuple[str, str]] = []
    for block in machine_function.blocks:
        nodes.append(block.name)
        for successor in block.successors:
            edges.append((block.name, successor))
    return _render_dot(nodes, edges)


def _render_dot(nodes: list[str], edges: list[tuple[str, str]]) -> str:
    def quote(name: str) -> str:
        return '"' + name.replace('"', '\\"') + '"'

    parts = ["digraph {", '  rankdir="TB";']
    parts.extend(f"  n{i} [label={quote(node)}];" for i, node in enumerate(nodes))
    index = {node: i for i, node in enumerate(nodes)}
    for src, dst in edges:
        if src in index and dst in index:
            parts.append(f"  n{index[src]} -> n{index[dst]};")
    parts.append("}")
    return "\n".join(parts)
