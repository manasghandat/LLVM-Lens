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
    assert 'n0 [label="entry"]' in dot
    assert 'n1 [label="then"]' in dot
    assert "n0 -> n1" in dot
    assert "n0 -> n2" in dot  # else
    assert "n1 -> n3" not in dot  # ret has no successors


def test_ir_cfg_switch_continuation_labels():
    dot = ir_cfg_dot(IR_WITH_SWITCH, "g")
    assert "n0 -> n1" in dot  # entry -> case0
    assert "n0 -> n2" in dot  # entry -> case1
    assert "n0 -> n3" in dot  # entry -> default


# --- cfg: machine ----------------------------------------------------------------


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


def test_machine_cfg_dot():
    dot = machine_cfg_dot(_machine_function())
    assert 'n0 [label="bb.0"]' in dot
    assert "n0 -> n1" in dot
    assert "n0 -> n2" in dot
    assert "n1 -> n2" in dot
    assert "n2 -> n2" not in dot
