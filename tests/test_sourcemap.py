"""Source-correlation parsing: debug metadata tables and snapshot mapping."""

from __future__ import annotations

from llvm_lens.parsers.print_changed import parse_changed_ir
from llvm_lens.sourcemap import (
    MIR_REF_RE,
    SourceRef,
    encode,
    has_debug_info,
    map_lines,
    parse_debug_table,
)

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
; ModuleID = 'sample.ll'
source_filename = "sample.c"
define i64 @getTime() !dbg !10 {
  ret i64 0, !dbg !30
}
!0 = !DIFile(filename: "sample.c", directory: "/repo")
!10 = distinct !DISubprogram(name: "getTime", file: !0, line: 12)
!30 = !DILocation(line: 14, column: 3, scope: !10)
Running pass: SimplifyCFGPass on main
*** IR Dump After SimplifyCFGPass on main ***
; ModuleID = 'sample.ll'
define i32 @main() {
  ret i32 0
}
"""


def test_a_module_scope_dump_carries_the_table_that_resolves_it():
    dumps = parse_changed_ir(MODULE_SCOPE_LOG)
    assert [(d.pass_name, d.function) for d in dumps] == [
        ("SROAPass", "getTime"), ("SimplifyCFGPass", "main"),
    ]
    assert "!DIFile" in dumps[0].metadata
    assert "Running pass:" not in dumps[0].metadata
    # The metadata is out of the code half but still paired with it.
    assert "!DIFile" not in dumps[0].ir
    assert map_lines(dumps[0].ir, parse_debug_table(dumps[0].metadata)) == [
        SourceRef("/repo/sample.c", 12),   # define ... !dbg !10
        SourceRef("/repo/sample.c", 14),   # ret ... !dbg !30
        None,                              # closing brace
    ]
    assert parse_debug_table(dumps[1].metadata) == {}


def test_has_debug_info():
    assert has_debug_info(MODULE)
    assert not has_debug_info("define i32 @main() {\n  ret i32 0\n}\n")
