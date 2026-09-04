"""DOT generation for IR control-flow graphs and machine CFGs.

IR CFGs are parsed from function text (block labels + terminator successors);
machine CFGs come from the ``successors:`` lists of MIR blocks. DOT strings
are stored in the report JSON and rendered client-side with cytoscape +
dagre. Each node carries a truncated ``label`` (a few instructions) for the
graph display, a full ``code`` attribute so the viewer can show the complete
block body on demand, and a ``name`` attribute holding the block label, which
identifies the block in that expanded view but is not drawn in the graph.
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
# An unnamed parameter, as a dump prints it ("%0"); the count of them decides
# which slot number an unnamed entry block got.
UNNAMED_VALUE_RE = re.compile(r"%\d+\b")

# Graph readability caps: node labels show up to MAX_CODE_LINES instructions,
# each truncated to MAX_CODE_CHARS. The full untruncated lines go into the
# node's ``code`` attribute.
MAX_CODE_LINES = 6
MAX_CODE_CHARS = 52
# Drop debug metadata so the visible code is mostly operands: IR lines carry
# ", !dbg !36" tails; MIR lines carry "debug-location !19" tokens and
# "; file.c:line:col" source comments.
IR_DBG_TAIL_RE = re.compile(r",?\s+!dbg\s+![^\s,]+.*$")
MIR_TAIL_RE = re.compile(r"debug-location\s+!\d+\s*")


def _truncate(text: str) -> str:
    """Shorten one instruction for the node label; full text goes in code."""
    if len(text) > MAX_CODE_CHARS:
        return text[: MAX_CODE_CHARS - 1] + "…"
    return text


def _clean_ir_line(line: str) -> str | None:
    """One IR line prepared for the CFG; None for comments/debug intrinsics."""
    text = line.strip()
    if text.startswith(";") or text.startswith("#dbg_"):
        return None
    return IR_DBG_TAIL_RE.sub("", line).strip()


def _parameter_list(define_line: str) -> str:
    """The text between a define's parentheses, nested types included."""
    start = define_line.find("(")
    if start < 0:
        return ""
    depth = 0
    for i, char in enumerate(define_line[start:], start):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return define_line[start + 1 : i]
    return ""


def _implicit_entry_name(define_line: str) -> str:
    """The entry block's label, which LLVM omits when the block is unnamed.

    Unnamed values are numbered in order: the parameters take the first slots
    and the entry block the next one, so the entry of
    ``define i64 @f(ptr %0)`` is ``%1``. That is the number the function's own
    br targets and phi predecessors use to refer back to it, so naming the
    block this way is what lets a back edge into the entry resolve.
    """
    return str(len(UNNAMED_VALUE_RE.findall(_parameter_list(define_line))))


def _clean_mir_line(line: str) -> str | None:
    """One MIR line prepared for the CFG; None for comments/DBG_VALUE."""
    text = line.strip()
    if not text or text.startswith(";") or text.startswith("DBG_VALUE"):
        return None
    return MIR_TAIL_RE.sub("", line.split(";", 1)[0]).strip()


def ir_cfg_dot(function_ir: str, function_name: str = "") -> str:
    """Build a DOT graph for one function's CFG from its IR text.

    Node labels carry up to MAX_CODE_LINES instructions so the graph shows
    the actual code in each block; the ``code`` attribute carries every
    instruction line untruncated for the full-body view.
    """
    nodes: list[tuple[str, list[str]]] = []
    edges: list[tuple[str, str]] = []
    current: str | None = None
    pending: list[str] = []
    code: list[str] = []
    define_line: str | None = None

    def flush() -> None:
        nonlocal pending
        if current is not None:
            edges.extend((current, successor) for successor in pending)
        pending = []

    for line in function_ir.splitlines():
        if line.startswith(("declare", "attributes")):
            continue
        if line.startswith("define"):
            define_line = line
            continue
        label = LABEL_RE.match(line)
        if label:
            flush()  # block boundary: the previous block's terminators are done
            if current is not None:
                nodes.append((current, code))  # the finished block keeps its code
            current = label.group(1)
            code = []
            continue
        if not line.strip():
            continue
        if line.strip() == "}":
            break  # function terminator: nothing after it belongs to a block
        if current is None:
            # No label line has opened a block yet, so this is the entry block,
            # whose label LLVM omits when it is unnamed. Without this the entry
            # block is missing from every CFG -- and a function that is one
            # unnamed block has no CFG at all.
            if define_line is None:
                continue  # not inside a function body
            current = _implicit_entry_name(define_line)
            code = []
        pending.extend(BR_LABEL_RE.findall(line))
        cleaned = _clean_ir_line(line)
        if cleaned is not None:
            code.append(cleaned)
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

    Node labels carry up to MAX_CODE_LINES instructions.
    """
    nodes: list[tuple[str, list[str]]] = []
    edges: list[tuple[str, str]] = []
    for block in machine_function.blocks:
        code = [
            cleaned
            for line in block.lines
            if (cleaned := _clean_mir_line(line)) is not None
        ]
        nodes.append((block.name, code))
    names = [name for name, _ in nodes]
    for block in machine_function.blocks:
        for successor in block.successors:
            target = _resolve_successor(successor, names)
            if target is not None:
                edges.append((block.name, target))
    return _render_dot(nodes, edges)


def _render_dot(nodes: list[tuple[str, list[str]]], edges: list[tuple[str, str]]) -> str:
    def escape(text: str) -> str:
        # Backslash and quote first, then newlines -> DOT's \n escape (which
        # the frontend parser turns back into real newlines).
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    parts = ["digraph {", '  rankdir="TB";']
    for i, (name, lines) in enumerate(nodes):
        # The block name travels in its own attribute and is not part of the
        # label: drawn inside the node it reads as a stray instruction -- a bare
        # "6" or "bb.1" sitting above the block body. The viewer uses it to name
        # the block in the expanded block view instead. With no name heading
        # them, the label's instructions no longer need their leading indent.
        display = [_truncate(line) for line in lines[:MAX_CODE_LINES]]
        attrs = [f"name={escape(name)}"]
        if display:
            attrs.append(f'label={escape("\n".join(display))}')
        if lines:
            attrs.append(f'code={escape("\n".join(lines))}')
        parts.append(f"  n{i} [{', '.join(attrs)}];")
    index = {name: i for i, (name, _) in enumerate(nodes)}
    for src, dst in edges:
        if src in index and dst in index:
            parts.append(f"  n{index[src]} -> n{index[dst]};")
    parts.append("}")
    return "\n".join(parts)
