"""DOT generation for IR control-flow graphs and machine CFGs."""

from __future__ import annotations

import re

from .parsers.mir import MachineFunction

# A block label at column 0, e.g. "entry:" / ".lr.ph:".
LABEL_RE = re.compile(r"^([.\w\"$%-]+):\s*(;.*)?$")
# "label %x" tokens appear only in terminators.
BR_LABEL_RE = re.compile(r"label %([.\w\"$-]+)")
# An unnamed parameter, as a dump prints it ("%0").
UNNAMED_VALUE_RE = re.compile(r"%\d+\b")

# Graph readability caps: up to MAX_CODE_LINES instructions, truncated to MAX_CODE_CHARS.
MAX_CODE_LINES = 6
MAX_CODE_CHARS = 52
# Drop debug metadata so visible code is mostly operands.
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
    """The entry block's implicit label when LLVM omits it (unnamed)."""
    return str(len(UNNAMED_VALUE_RE.findall(_parameter_list(define_line))))


def _clean_mir_line(line: str) -> str | None:
    """One MIR line prepared for the CFG; None for comments/DBG_VALUE."""
    text = line.strip()
    if not text or text.startswith(";") or text.startswith("DBG_VALUE"):
        return None
    return MIR_TAIL_RE.sub("", line.split(";", 1)[0]).strip()


def ir_cfg_dot(function_ir: str, function_name: str = "") -> str:
    """Build a DOT graph for one function's CFG from its IR text."""
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
            # No label yet: this is the (implicitly named) entry block.
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
    """Map a short successor name to its full block name."""
    if short in node_names:
        return short
    matches = [name for name in node_names if name.startswith(short + ".")]
    return matches[0] if len(matches) == 1 else None


def machine_cfg_dot(machine_function: MachineFunction) -> str:
    """Build a DOT graph for one function's machine CFG from MIR blocks."""
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
        # Backslash and quote first, then newlines -> DOT's \n escape.
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'

    parts = ["digraph {", '  rankdir="TB";']
    for i, (name, lines) in enumerate(nodes):
        # Block name travels in its own attribute, not the drawn label.
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
