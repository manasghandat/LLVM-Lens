"""Parse llc's ``-debug-pass=Structure`` output (backend lane).

Shape (LLVM 22, stderr):

    Pass Arguments:  -targetlibinfo -runtime-library-info ...
    Target Library Information
    ...
      ModulePass Manager
        Pre-ISel Intrinsic Lowering
        FunctionPass Manager
          Expand IR instructions
          ...

Indentation is two spaces per nesting level. The flat, ordered pass list is
the primary output; depth keeps the nesting for display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PASS_ARGS_RE = re.compile(r"^Pass Arguments: ?(.*)$")
# Two-space indentation, optional legacy "Pass: " prefix, then the name.
PASS_LINE_RE = re.compile(r"^(?: {2})*(?:Pass: )?(.+)$")


@dataclass(frozen=True)
class PassNode:
    name: str
    depth: int
    line: int  # 1-based line in the stream


def parse_pass_structure(stderr: str) -> tuple[list[PassNode], str | None]:
    """Return (ordered pass nodes, pass-arguments string)."""
    nodes: list[PassNode] = []
    pass_arguments: str | None = None
    for line_no, line in enumerate(stderr.splitlines(), start=1):
        match = PASS_ARGS_RE.match(line)
        if match:
            pass_arguments = match.group(1).strip()
            continue
        if not line.strip():
            continue
        if "Pass execution timing report" in line:
            continue
        depth = (len(line) - len(line.lstrip(" "))) // 2
        stripped = line.strip()
        if stripped.startswith("Pass: "):
            stripped = stripped[len("Pass: "):]
        if stripped and not stripped.startswith("==="):
            nodes.append(PassNode(stripped, depth, line_no))
    return nodes, pass_arguments


def _is_manager(name: str) -> bool:
    """A structure node is a pass manager iff its name contains "Manager".

    LLVM names every pass manager "<...>Pass Manager" or "<...>Analysis Manager"
    (e.g. ModulePass Manager, FunctionPass Manager, Loop Pass Manager); no real
    pass or analysis has "Manager" in its name, so this is a robust classifier.
    """
    return "Manager" in name


def build_tree(
    nodes: list[PassNode],
    passes_by_name: dict[str, int],
) -> dict[str, object]:
    """Collapse the flat, execution-ordered structure trace into a compact tree.

    ``parse_pass_structure`` returns an *expanded* trace: every pass invocation is
    its own entry, so ``Print Function IR`` appears once per printed pass and the
    same analysis recurs many times. A pass's depth encodes which manager it runs
    under (ModulePass Manager > FunctionPass Manager > Loop Pass Manager > leaf),
    so a depth-stack walk recovers the manager skeleton. Leaves are **deduped by
    name** (first occurrence wins) to collapse the trace into the pipeline's
    actual structure.

    Machine-pass leaves are linked to their ``ReportPass`` (by id) via
    *passes_by_name*; analysis, print, and IR-level passes become structural nodes
    with ``passId`` null.
    """
    root: dict[str, object] = {
        "name": "__root__", "kind": "root", "depth": -1,
        "passId": None, "children": [],
    }
    stack: list[tuple[int, dict[str, object]]] = [(-1, root)]
    seen: set[str] = set()  # leaf names already emitted (dedup)
    for node in nodes:
        while stack[-1][0] >= node.depth:
            stack.pop()
        parent = stack[-1][1]
        manager = _is_manager(node.name)
        if not manager and node.name in seen:
            continue  # skip duplicate leaf invocation
        seen.add(node.name)
        child: dict[str, object] = {
            "name": node.name,
            "kind": "manager" if manager else "pass",
            "depth": node.depth,
            "passId": passes_by_name.get(node.name),
            "children": [],
        }
        parent["children"].append(child)  # type: ignore[union-attr]
        if manager:
            stack.append((node.depth, child))
    return root
