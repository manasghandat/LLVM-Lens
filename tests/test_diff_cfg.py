"""Tests for cli.diff (snapshot pairing) and cli.cfg (DOT generation)."""

from __future__ import annotations

from cli.cfg import ir_cfg_dot, machine_cfg_dot
from cli.diff import FnChange, pair_snapshots
from cli.parsers.mir import MachineBlock, MachineFunction
from cli.parsers.print_changed import IrSnapshot


def _snap(pass_name: str, function: str, ir: str) -> IrSnapshot:
    return IrSnapshot(pass_name, function, ir)


# --- diff ---------------------------------------------------------------------


def test_pair_snapshots_groups_by_function():
    changes = pair_snapshots([
        _snap("P1", "main", "define i32 @main()"),
        _snap("P1", "square", "define i32 @square()"),
        _snap("P2", "main", "define i32 @main() { ret i32 0 }"),
    ])
    main = changes["main"]
    assert [c.before for c in main] == ["", "define i32 @main()"]
    assert [c.changed for c in main] == [True, True]
    assert changes["square"][0].changed


def test_pair_snapshots_marks_unchanged():
    changes = pair_snapshots([
        _snap("P1", "main", "same"),
        _snap("P2", "main", "same"),
    ])
    assert changes["main"][1].changed is False


def test_fn_change_unified_diff():
    change = FnChange("f", "a\nb\n", "a\nc\n")
    diff = change.unified_diff()
    assert "-b" in diff
    assert "+c" in diff


# --- cfg: IR -------------------------------------------------------------------


IR_WITH_BRANCH = """define i32 @f(i32 %x) {
entry:
  %cmp = icmp sgt i32 %x, 0
  br i1 %cmp, label %then, label %else

then:
  ret i32 1

else:
  ret i32 0
}
"""

IR_WITH_SWITCH = """define i32 @g(i32 %x) {
entry:
  switch i32 %x, label %default [
    i32 0, label %case0
    i32 1, label %case1
  ]

case0:
  ret i32 0

case1:
  ret i32 1

default:
  ret i32 2
}
"""


def test_ir_cfg_branch():
    dot = ir_cfg_dot(IR_WITH_BRANCH, "f")
    assert dot.startswith("digraph")
    # Labels carry the block name plus its instructions (escaped as \n);
    # the code attribute carries the full untruncated block body.
    assert (
        'n0 [label="entry\\n  %cmp = icmp sgt i32 %x, 0\\n  br i1 %cmp, label %then, label %else", '
        'code="%cmp = icmp sgt i32 %x, 0\\nbr i1 %cmp, label %then, label %else"]'
    ) in dot
    assert 'n1 [label="then\\n  ret i32 1", code="ret i32 1"]' in dot
    assert "n0 -> n1" in dot
    assert "n0 -> n2" in dot  # else
    assert "n1 -> n3" not in dot  # ret has no successors


def test_ir_cfg_switch_continuation_labels():
    dot = ir_cfg_dot(IR_WITH_SWITCH, "g")
    assert "n0 -> n1" in dot  # entry -> case0
    assert "n0 -> n2" in dot  # entry -> case1
    assert "n0 -> n3" in dot  # entry -> default


# --- cfg: machine ----------------------------------------------------------------


def test_machine_cfg_dot_from_real_dump():
    """Real llc successors lines use dotted block names; edges must survive."""
    from cli.parsers.mir import parse_mir_snapshots

    dump = (
        "# *** IR Dump After X86 DAG->DAG Instruction Selection (x86-isel) ***:\n"
        "# Machine code for function main: IsSSA, TracksLiveness\n"
        "Function Live Ins: $edi in %5\n"
        "bb.0 (%ir-block.2):\n"
        "  successors: %bb.1..lr.ph.preheader(0x50000000), %bb.3.._crit_edge(0x30000000); %bb.1..lr.ph.preheader(62.50%), %bb.3.._crit_edge(37.50%)\n"
        "  %5:gr32 = MOV32rm $edi, 1, $noreg, 0, $noreg\n"
        "bb.1..lr.ph.preheader (%ir-block.3):\n"
        "  successors: %bb.2..lr.ph(0x80000000); %bb.2..lr.ph(100.00%)\n"
        "bb.2..lr.ph (%ir-block.4):\n"
        "  successors: %bb.2..lr.ph(0x40000000), %bb.3.._crit_edge(0x40000000); %bb.2..lr.ph(50.00%), %bb.3.._crit_edge(50.00%)\n"
        "  JCC_1 %bb.2..lr.ph, 5, implicit $eflags\n"
        "bb.3.._crit_edge (%ir-block.5):\n"
        "  RET 0\n"
        "# End machine code for function main.\n"
    )
    snapshots = parse_mir_snapshots(dump)
    mf = snapshots[0].functions["main"]
    assert mf.blocks[0].successors == ("bb.1", "bb.3")  # short form, deduped
    dot = machine_cfg_dot(mf)
    assert "n0 -> n1" in dot  # bb.0 -> bb.1..lr.ph.preheader
    assert "n0 -> n3" in dot  # bb.0 -> bb.3.._crit_edge
    assert "n2 -> n2" in dot  # loop back edge
    assert dot.count("->") == 5  # no duplicated edges


def _machine_function() -> MachineFunction:
    return MachineFunction(
        name="main",
        properties="IsSSA, TracksLiveness",
        live_ins="$edi in %5",
        blocks=(
            MachineBlock("bb.0", "2", ("bb.1", "bb.3"),
                         ("  %0:gr32 = MOV32rm ...",)),
            MachineBlock("bb.1", None, ("bb.3",), ()),
            MachineBlock("bb.3", "._crit_edge", (), ()),
        ),
    )


def test_machine_cfg_align16_block_headers():
    """Post-RA headers may carry '(%ir-block.N, align 16)'; they are blocks,
    not instructions, and their successors still resolve."""
    from cli.parsers.mir import parse_mir_snapshots

    dump = (
        "# *** IR Dump After X86 Assembly Printer (x86-asm-printer) ***:\n"
        "# Machine code for function main: NoPHIs, TracksLiveness, NoVRegs\n"
        "Function Live Ins: $r15 in %0\n"
        "bb.0 (%ir-block.0):\n"
        "  successors: %bb.1(0x80000000); %bb.1(100.00%)\n"
        "  JMP_1 %bb.1\n"
        "bb.1 (%ir-block.41, align 16):\n"
        "  successors: %bb.2(0x04000000), %bb.0(0x7c000000); %bb.2(3.12%), %bb.0(96.88%)\n"
        "  JCC_1 %bb.0, 4, implicit $eflags\n"
        "bb.2 (%ir-block.42, align 16):\n"
        "  RET 0\n"
        "# End machine code for function main.\n"
    )
    mf = parse_mir_snapshots(dump)[0].functions["main"]
    assert [b.name for b in mf.blocks] == ["bb.0", "bb.1", "bb.2"]
    dot = machine_cfg_dot(mf)
    assert "n0 -> n1" in dot
    assert "n1 -> n0" in dot  # loop back edge survived
    assert "n1 -> n2" in dot


def test_machine_cfg_dot():
    dot = machine_cfg_dot(_machine_function())
    # bb.0 shows its instruction; the empty block is just its name.
    assert 'n0 [label="bb.0\\n  %0:gr32 = MOV32rm ...", code="%0:gr32 = MOV32rm ..."]' in dot
    assert 'n1 [label="bb.1"]' in dot  # no code attribute for empty blocks
    assert "n0 -> n1" in dot
    assert "n0 -> n2" in dot
    assert "n1 -> n2" in dot
    assert "n2 -> n2" not in dot


def test_cfg_labels_capped_and_trimmed():
    from cli.cfg import MAX_CODE_CHARS, MAX_CODE_LINES

    ir = """define void @f() {
entry:
  %0 = add i32 0, 1, !dbg !1
  %1 = add i32 0, 2, !dbg !2
  %2 = add i32 0, 3, !dbg !3
  %3 = add i32 0, 4, !dbg !4
  %4 = add i32 0, 5, !dbg !5
  %5 = add i32 0, 6, !dbg !6
  %6 = add i32 0, 7, !dbg !7
  ret void
}
"""
    dot = ir_cfg_dot(ir)
    # !dbg tails dropped, label instructions capped at MAX_CODE_LINES...
    assert "!dbg" not in dot
    assert dot.count("\\n  %") == MAX_CODE_LINES
    # ...but the code attribute carries every instruction untruncated.
    assert 'code="%0 = add i32 0, 1\\n%1 = add i32 0, 2\\n%2 = add i32 0, 3\\n%3 = add i32 0, 4\\n%4 = add i32 0, 5\\n%5 = add i32 0, 6\\n%6 = add i32 0, 7\\nret void"' in dot

    long_line = "  " + "x" * 200 + " = %42, !dbg !1"
    dot = ir_cfg_dot("define void @f() {\nentry:\n" + long_line + "\n  ret void\n}\n")
    assert "…" in dot  # the label truncates...
    assert 'code="' + "x" * 200 + ' = %42\\nret void"' in dot  # ...the code attribute does not

    # Machine lines: debug-location and source comments cut, memoperands kept.
    mf = MachineFunction(
        name="f", properties="", live_ins="",
        blocks=(MachineBlock(
            "bb.0", None, (), (
                "  %0 = MOV32rm $edi, debug-location !19 :: (load (s32) from %stack.4); f.c:3:1",
                "  ; comment-only line stays out of the graph",
                "  DBG_VALUE $edi, $noreg, !\"argc\", !DIExpression(), debug-location !19",
            ),
        ),),
    )
    dot = machine_cfg_dot(mf)
    assert "debug-location" not in dot
    assert "; f.c:3:1" not in dot
    assert "%stack.4" in dot  # spill annotation preserved
    assert "comment-only" not in dot
    assert "DBG_VALUE" not in dot  # debug pseudo-instructions stay out
    # Full cleaned instruction line in the code attribute.
    assert ', code="%0 = MOV32rm $edi, :: (load (s32) from %stack.4)"]' in dot
