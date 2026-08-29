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
