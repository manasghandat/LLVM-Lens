"""Source correlation: map IR/MIR lines back to the original source."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain

# "!180 = distinct !DILocation(line: 77, ...)"; MIR indents its embedded module.
NODE_RE = re.compile(r"^\s*!(\d+) = (?:distinct )?!(\w+)\((.*)\)\s*$")
FIELD_RE = re.compile(r"\b(\w+): (?:!(\d+)|(\d+)|\"((?:[^\"\\]|\\.)*)\")")

IR_REF_RE = re.compile(r"!dbg !(\d+)")
MIR_REF_RE = re.compile(r"debug-location !(\d+)")

# Nodes that name a source line directly.
LINE_BEARING = ("DILocation", "DISubprogram", "DILabel", "DIGlobalVariable")

# Machine pass to stop at when harvesting the backend's metadata table.
MIR_STOP_AFTER = "finalize-isel"


@dataclass(frozen=True)
class SourceRef:
    """A resolved debug location: which file, which 1-based line."""

    file: str
    line: int


# One printed module state's `!N` -> location table.
DebugTable = dict[int, SourceRef]

# Per text line of a snapshot: its location, or None where there is no !dbg.
LineMap = list[SourceRef | None]


def _fields(args: str) -> dict[str, str | int]:
    """Parse a metadata node's argument list into {name: value}."""
    out: dict[str, str | int] = {}
    for match in FIELD_RE.finditer(args):
        name, ref, number, text = match.groups()
        if ref is not None:
            out[name] = f"!{ref}"
        elif number is not None:
            out[name] = int(number)
        else:
            out[name] = (text or "").replace('\\"', '"')
    return out


def parse_debug_table(module_text: str) -> DebugTable:
    """Build the `!N` -> SourceRef table of one printed module."""
    nodes: dict[int, tuple[str, dict[str, str | int]]] = {}
    for line in module_text.splitlines():
        match = NODE_RE.match(line)
        if match:
            nodes[int(match.group(1))] = (match.group(2), _fields(match.group(3)))

    files: dict[int, str] = {}

    def file_of(node: int, seen: frozenset[int] = frozenset()) -> str | None:
        """Walk file: / scope: links until a DIFile turns up."""
        if node in files:
            return files[node]
        if node in seen or node not in nodes:
            return None
        kind, args = nodes[node]
        if kind == "DIFile":
            name = str(args.get("filename", ""))
            directory = str(args.get("directory", ""))
            path = str(Path(directory) / name) if directory and not Path(name).is_absolute() else name
            files[node] = path
            return path
        for link in ("file", "scope"):
            target = args.get(link)
            if isinstance(target, str) and target.startswith("!"):
                found = file_of(int(target[1:]), seen | {node})
                if found:
                    return found
        return None

    table: DebugTable = {}
    for number, (kind, args) in nodes.items():
        if kind == "DIGlobalVariableExpression":
            var = args.get("var")
            if isinstance(var, str) and var.startswith("!"):
                target = table.get(int(var[1:]))
                if target:
                    table[number] = target
            continue
        if kind not in LINE_BEARING:
            continue
        line = args.get("line")
        if not isinstance(line, int) or line <= 0:
            continue
        scope = args.get("scope") or args.get("file")
        path = file_of(int(str(scope)[1:])) if isinstance(scope, str) else None
        if path:
            table[number] = SourceRef(path, line)

    # DIGlobalVariableExpression may be numbered before the variable it names.
    for number, (kind, args) in nodes.items():
        if kind != "DIGlobalVariableExpression" or number in table:
            continue
        var = args.get("var")
        if isinstance(var, str) and var.startswith("!"):
            target = table.get(int(var[1:]))
            if target:
                table[number] = target
    return table


def map_lines(text: str, table: DebugTable, ref_re: re.Pattern[str] = IR_REF_RE) -> LineMap:
    """Resolve every line of a snapshot to its source location (None if any)."""
    mapping: LineMap = []
    for line in text.splitlines():
        match = ref_re.search(line)
        mapping.append(table.get(int(match.group(1))) if match else None)
    return mapping


def has_debug_info(ir_text: str) -> bool:
    """True when the module carries the !DILocation nodes we map through."""
    return "!DILocation(" in ir_text


# --- backend harvest ----------------------------------------------------------


def harvest_mir_table(
    input_ir: str | Path,
    toolchain: Toolchain,
    load: tuple[str, ...] = (),
    timeout: float | None = None,
    extra_args: tuple[str, ...] = (),
) -> DebugTable:
    """Harvest the backend's metadata table from a stop-after MIR dump."""
    cmd = [str(toolchain.llc.path), f"-stop-after={MIR_STOP_AFTER}", "-o", "-"]
    cmd.extend(f"-load={plugin}" for plugin in load)
    cmd.extend(extra_args)
    cmd.append(str(input_ir))
    try:
        result = run_capture(cmd, timeout)
    except ProcError:
        return {}
    if result.timed_out or result.returncode != 0:
        return {}
    return parse_debug_table(result.stdout)


# --- report payload -----------------------------------------------------------


def read_sources(maps: list[LineMap], limit: int = 2_000_000) -> dict[str, str]:
    """Read every source file the mappings point at, skipping unreadable ones."""
    wanted = {ref.file for mapping in maps for ref in mapping if ref}
    texts: dict[str, str] = {}
    for path in sorted(wanted):
        try:
            file = Path(path)
            if file.is_file() and file.stat().st_size <= limit:
                texts[path] = file.read_text(errors="replace")
        except OSError:
            continue
    return texts


def encode(mapping: LineMap, files: list[str]) -> list[list[int] | None]:
    """Per-line [file index, source line], or None -- the report's wire form."""
    index = {path: i for i, path in enumerate(files)}
    return [
        [index[ref.file], ref.line] if ref and ref.file in index else None
        for ref in mapping
    ]
