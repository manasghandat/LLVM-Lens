"""Source-correlation parsing: debug metadata tables and snapshot mapping.

Uses canned module text rather than a live toolchain — the shapes here are
exactly what `opt -print-module-scope` and `llc -stop-after` print on LLVM 22.
"""

from __future__ import annotations

from cli.main import _join_ir_tables
from cli.parsers.print_changed import IrSnapshot
from cli.sourcemap import (
    MIR_REF_RE,
    SourceRef,
    encode,
    has_debug_info,
    map_lines,
    module_scope_dumps,
    parse_debug_table,
)

# A module tail carrying every node shape the resolver walks: a plain
# location, one inlined, one inside a lexical block, a line-0 marker, the
# subprogram a `define ... !dbg` header points at, and a global's variable.
MODULE = """\
@flag = global [64 x i8] zeroinitializer, !dbg !20
define i32 @main() !dbg !10 {
  %1 = add i32 1, 2, !dbg !30
  %2 = mul i32 %1, 3, !dbg !31
  %3 = phi i32 [ %1, %a ], [ %2, %b ], !dbg !32
  ret i32 %3, !dbg !33
}
!0 = !DIFile(filename: "sample.c", directory: "/repo")
!1 = !DIFile(filename: "/abs/other.h", directory: "/repo")
!10 = distinct !DISubprogram(name: "main", scope: !0, file: !0, line: 30)
!11 = distinct !DISubprogram(name: "helper", scope: !1, file: !1, line: 4)
!12 = distinct !DILexicalBlock(scope: !10, file: !0, line: 63, column: 9)
!20 = !DIGlobalVariableExpression(var: !21, expr: !DIExpression())
!21 = distinct !DIGlobalVariable(name: "flag", scope: !0, file: !0, line: 21)
!30 = !DILocation(line: 77, column: 5, scope: !10)
!31 = !DILocation(line: 9, column: 3, scope: !11, inlinedAt: !30)
!32 = !DILocation(line: 0, scope: !12)
!33 = !DILocation(line: 65, column: 36, scope: !12)
"""


def test_parse_debug_table_resolves_scopes_to_files():
    table = parse_debug_table(MODULE)
    assert table[30] == SourceRef("/repo/sample.c", 77)
    # inlined body: the line belongs to the inlined function's own file
    assert table[31] == SourceRef("/abs/other.h", 9)
    # a lexical block has no line of its own; the scope chain finds the file
    assert table[33] == SourceRef("/repo/sample.c", 65)


def test_parse_debug_table_covers_declarations():
    table = parse_debug_table(MODULE)
    assert table[10] == SourceRef("/repo/sample.c", 30)   # define ... !dbg !10
    assert table[20] == SourceRef("/repo/sample.c", 21)   # global's expression
    assert table[21] == SourceRef("/repo/sample.c", 21)


def test_line_zero_is_not_a_source_location():
    # LLVM marks compiler-synthesized instructions (phi merges, prologue) with
    # line 0; mapping those to line 0 of the file would be a lie.
    assert 32 not in parse_debug_table(MODULE)


def test_absolute_filename_ignores_the_directory():
    assert parse_debug_table(MODULE)[31].file == "/abs/other.h"


def test_map_lines_aligns_with_the_snapshot_text():
    table = parse_debug_table(MODULE)
    body = "\n".join(MODULE.splitlines()[1:7])
    mapping = map_lines(body, table)
    assert len(mapping) == len(body.splitlines())
    assert mapping[0] == SourceRef("/repo/sample.c", 30)   # the define line
    assert mapping[1] == SourceRef("/repo/sample.c", 77)
    assert mapping[3] is None                              # the line-0 phi
    assert mapping[5] is None                              # closing brace


def test_map_lines_reads_mir_debug_locations():
    table = parse_debug_table(MODULE)
    mir = "  $eax = MOV32ri 7, debug-location !30\n  RET64 implicit $eax\n"
    assert map_lines(mir, table, MIR_REF_RE) == [SourceRef("/repo/sample.c", 77), None]


def test_encode_indexes_files_and_drops_unknown_ones():
    files = ["/repo/sample.c", "/abs/other.h"]
    mapping = [SourceRef("/abs/other.h", 9), None, SourceRef("/gone.c", 1)]
    assert encode(mapping, files) == [[1, 9], None, None]


MODULE_SCOPE_LOG = """\
*** IR Dump After SROAPass on getTime ***
define i64 @getTime() {
  ret i64 0
}
!0 = !DIFile(filename: "sample.c", directory: "/repo")
Running pass: SimplifyCFGPass on main
*** IR Dump After SimplifyCFGPass on main ***
define i32 @main() {
  ret i32 0
}
"""


def test_module_scope_dumps_keep_whole_modules():
    dumps = module_scope_dumps(MODULE_SCOPE_LOG)
    assert [(name, fn) for name, fn, _ in dumps] == [
        ("SROAPass", "getTime"), ("SimplifyCFGPass", "main"),
    ]
    # The body must not stop at the function's "}" — the metadata table that
    # follows it is the whole point of the module-scope capture.
    assert "!DIFile" in dumps[0][2]
    assert "Running pass:" not in dumps[0][2]


def test_has_debug_info():
    assert has_debug_info(MODULE)
    assert not has_debug_info("define i32 @main() {\n  ret i32 0\n}\n")


def _snapshot(pass_name, function):
    return IrSnapshot(pass_name, function, "")


def test_join_ir_tables_pairs_dumps_by_position():
    dumps = [_snapshot("A", "f"), _snapshot("B", "g")]
    tables = [("A", "f", {1: SourceRef("x", 1)}), ("B", "g", {})]
    assert _join_ir_tables(dumps, tables) == [{1: SourceRef("x", 1)}, {}]


def test_join_ir_tables_drops_a_harvest_that_diverged():
    # A harvest that ran a different pipeline would silently shift every
    # mapping by one; dropping it entirely is the only safe answer.
    dumps = [_snapshot("A", "f"), _snapshot("B", "g")]
    assert _join_ir_tables(dumps, [("A", "f", {}), ("C", "g", {})]) == [None, None]
    assert _join_ir_tables(dumps, [("A", "f", {})]) == [None, None]
    assert _join_ir_tables(dumps, None) == [None, None]
