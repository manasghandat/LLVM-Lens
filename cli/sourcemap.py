"""Source correlation: map IR / Machine IR lines back to the original source.

Under ``-g`` every instruction carries a ``!dbg !N`` (IR) or
``debug-location !N`` (MIR) reference to a ``!DILocation``, but the *definition*
of that node is only printed at module scope -- a ``-print-changed=quiet``
function dump references ``!N`` without defining it. Worse, opt renumbers
metadata as passes drop it, so no single table serves the whole pipeline: the
same ``ret i32 0`` is ``!dbg !237`` after SimplifyCFG, ``!212`` after SROA and
``!180`` from GVN onwards.

Two harvests fix that, both self-describing:

  Lane A  a second opt run with the same pipeline plus ``-print-module-scope``.
          Its dump sequence is header-for-header identical to the main run's,
          and the function bodies inside are byte-identical (same slot
          numbering), so dump *k*'s module carries exactly the table that
          resolves snapshot *k*.
  Lane B  one ``llc -stop-after=finalize-isel``, whose MIR output embeds the
          module and its metadata. llc never renumbers mid-backend, so that one
          table resolves every machine pass.

Both are skipped when the input carries no debug info, and a failed harvest
degrades to "no mapping" rather than an error: source correlation is an
enrichment, never a precondition for the report.

Metadata shapes consumed (LLVM 22)::

    !180 = !DILocation(line: 77, column: 5, scope: !104)
    !181 = !DILocation(line: 9, column: 3, scope: !88, inlinedAt: !180)
    !104 = distinct !DISubprogram(name: "main", file: !1, line: 30, ...)
    !88  = distinct !DILexicalBlock(scope: !104, file: !1, line: 63, column: 9)
    !1   = !DIFile(filename: "tests/fixtures/sample.c", directory: "/repo")
    !4   = !DIGlobalVariableExpression(var: !5, expr: !DIExpression())
    !5   = distinct !DIGlobalVariable(name: "flag", file: !1, line: 21, ...)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain

# "*** IR Dump After SimplifyCFGPass on main ***" -- same header at either scope.
HEADER_RE = re.compile(r"^\*\*\* IR Dump After (.+?) on (.+) \*\*\*$")
# -debug-pass-manager chatter interleaved with the dumps; never IR.
NOISE_RE = re.compile(r"^(?:Running (?:pass|analysis)|Invalidating analysis):")
# "!180 = distinct !DILocation(line: 77, ...)"; MIR indents its embedded module.
NODE_RE = re.compile(r"^\s*!(\d+) = (?:distinct )?!(\w+)\((.*)\)\s*$")
FIELD_RE = re.compile(r"\b(\w+): (?:!(\d+)|(\d+)|\"((?:[^\"\\]|\\.)*)\")")

IR_REF_RE = re.compile(r"!dbg !(\d+)")
MIR_REF_RE = re.compile(r"debug-location !(\d+)")

# Nodes that name a source line directly. DILocation covers instructions;
# the other two let a `define ... !dbg !49` header and a global's attachment
# point at their declaration line instead of going unmapped.
LINE_BEARING = ("DILocation", "DISubprogram", "DILabel", "DIGlobalVariable")

# Machine pass to stop at when harvesting the backend's metadata table. Every
# target runs it, and stopping there is far cheaper than a full codegen.
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
    """Parse a metadata node's argument list into {name: value}.

    Metadata references keep their ``!`` prefix ("!104"), integers come back as
    ints, strings unquoted. Nested nodes (``expr: !DIExpression()``) are values
    we never need, so they simply do not match.
    """
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
    """Build the `!N` -> SourceRef table of one printed module.

    Only nodes that name a line end up in the table; the rest of the debug
    graph is walked to answer "which file is this scope in".
    """
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


def module_scope_dumps(stderr: str) -> list[tuple[str, str, str]]:
    """Split a ``-print-module-scope`` capture into (pass, function, module).

    Unlike parsers/print_changed.py this never ends a body at ``}``: every
    dump here is a whole module, so bodies run to the next header.
    """
    dumps: list[tuple[str, str, list[str]]] = []
    current: list[str] | None = None
    for line in stderr.splitlines():
        match = HEADER_RE.match(line)
        if match:
            current = []
            dumps.append((match.group(1), match.group(2), current))
            continue
        if current is None or NOISE_RE.match(line):
            continue
        current.append(line)
    return [(name, function, "\n".join(body)) for name, function, body in dumps]


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


# --- harvests -----------------------------------------------------------------


def harvest_ir_tables(
    input_ir: str | Path,
    passes: str,
    toolchain: Toolchain,
    load_pass_plugins: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    timeout: float | None = None,
) -> list[tuple[str, str, DebugTable]]:
    """Re-run opt at module scope; return (pass, function, table) per dump.

    The pipeline must match the main run exactly or the dump sequences stop
    lining up -- the caller joins them by position and verifies the headers.
    Returns ``[]`` on any failure: no mapping beats a wrong one.
    """
    from .runner_opt import opt_command  # local: avoids an import cycle

    cmd = opt_command(
        toolchain.opt.path, Path(input_ir), passes,
        out=Path("/dev/null"),
        load_pass_plugins=load_pass_plugins, print_after=print_after,
        extra_args=("-print-module-scope",),
    )
    try:
        result = run_capture(cmd, timeout)
    except ProcError:
        return []
    if result.timed_out:
        return []
    return [
        (name, function, parse_debug_table(module))
        for name, function, module in module_scope_dumps(result.stderr)
    ]


def harvest_mir_table(
    input_ir: str | Path,
    toolchain: Toolchain,
    load: tuple[str, ...] = (),
    timeout: float | None = None,
) -> DebugTable:
    """Harvest the backend's metadata table from a stop-after MIR dump.

    llc numbers metadata once for the module it reads and never renumbers, so
    this single table resolves ``debug-location`` in every machine pass.
    """
    cmd = [str(toolchain.llc.path), f"-stop-after={MIR_STOP_AFTER}", "-o", "-"]
    cmd.extend(f"-load={plugin}" for plugin in load)
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
