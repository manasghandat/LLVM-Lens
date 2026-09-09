"""IR <-> machine IR correlation at instruction selection."""

from llvm_lens.isel import correlate, ir_block_spans, mir_block_spans
from llvm_lens.parsers.mir import parse_ir_dumps
from llvm_lens.report import build_lane_b

# One function through llc: the IR dump its last pre-ISel pass printed, then the
# machine code ISel made of it. Trimmed, but header-for-header what llc writes.
LLC_STDERR = """\
*** IR Dump After Canonicalize natural loops (loop-simplify) ***
define i64 @f(ptr %0, i32 %1) {
  %3 = load i64, ptr %0, align 8
  br label %body

body:                                             ; preds = %2
  %4 = add i64 %3, 1
  br label %exit

exit:                                             ; preds = %body
  ret i64 %4
}
*** IR Dump After Module Verifier (verify) ***
define i64 @f(ptr %0, i32 %1) {
  %3 = load i64, ptr %0, align 8
  br label %body

body:                                             ; preds = %2
  %4 = add i64 %3, 1
  br label %exit

exit:                                             ; preds = %body
  ret i64 %4

dead:                                             ; preds = %2
  unreachable
}
# *** IR Dump After X86 DAG->DAG Instruction Selection (x86-isel) ***:
# Machine code for function f: IsSSA, TracksLiveness
Function Live Ins: $rdi in %7

bb.0 (%ir-block.2):
  successors: %bb.1(0x80000000)
  %9:gr64 = MOV64rm %7:gr64, 1, $noreg, 0, $noreg :: (load (s64) from %ir.0)
  JMP_1 %bb.1
bb.1.body:
  successors: %bb.2(0x80000000)
  %10:gr64 = ADD64ri %9:gr64, 1, implicit-def dead $eflags
bb.2.exit:
  $rax = COPY %10:gr64
  RET64 $rax
bb.3:
  INT3
# End machine code for function f.
# *** IR Dump After Finalize ISel (finalize-isel) ***:
# Machine code for function f: IsSSA, TracksLiveness
Function Live Ins: $rdi in %7

bb.0 (%ir-block.2):
  RET64 $rax
# End machine code for function f.
"""

IR_FUNCTION = """\
define i64 @f(ptr %0, i32 %1) {
  %3 = load i64, ptr %0, align 8
  br label %body

body:                                             ; preds = %2
  %4 = add i64 %3, 1
  br label %exit

exit:                                             ; preds = %body
  ret i64 %4
}"""


def test_ir_block_spans_open_the_entry_at_its_first_instruction():
    """The entry block has no label line, so it starts at the first instruction
    and takes the slot number the rest of the function refers to it by."""
    spans = ir_block_spans(IR_FUNCTION)
    assert [s.name for s in spans] == ["2", "body", "exit"]
    assert spans[0].start == 1  # the line after "define", not the define itself
    assert spans[0].end == 3  # up to the blank line before "body:"
    assert spans[-1].end == 9  # the "}" is not part of the last block


def test_mir_block_spans_name_their_ir_block_both_ways():
    """LLVM folds a named IR block into the machine block's own name and falls
    back to a parenthesised slot for an unnamed one; a block the backend made
    up has neither."""
    mir = LLC_STDERR.split("***:\n", 1)[1].split("# End machine", 1)[0]
    spans = mir_block_spans(mir)
    assert [(s.name, ir) for s, ir in spans] == [
        ("bb.0", "2"),  # "(%ir-block.2)": the IR entry has no name to borrow
        ("bb.1.body", "body"),
        ("bb.2.exit", "exit"),
        ("bb.3", None),  # no IR block behind it
    ]


def test_correlate_pairs_blocks_and_value_references():
    mir = LLC_STDERR.split("***:\n", 1)[1].split("# End machine", 1)[0]
    corr = correlate(IR_FUNCTION, mir)
    assert [b["name"] for b in corr["irBlocks"]] == ["2", "body", "exit"]
    assert [b["irBlock"] for b in corr["mirBlocks"]] == [0, 1, 2, None]
    # ":: (load (s64) from %ir.0)" names %0, a parameter, so it points at the
    # define line that declares it.
    ref_lines = {int(k): v for k, v in corr["refs"].items()}
    assert ref_lines and all(v == [0] for v in ref_lines.values())
    assert "load (s64) from %ir.0" in mir.splitlines()[ref_lines.popitem()[0]]


def test_correlate_declines_when_there_is_nothing_to_pair():
    assert correlate(IR_FUNCTION, "# Machine code for function f: IsSSA\n") is None
    assert correlate("", "bb.0:\n  RET64\n") is None


def test_parse_ir_dumps_stop_at_the_machine_dumps():
    dumps = parse_ir_dumps(LLC_STDERR)
    assert [d.pass_id for d in dumps] == ["loop-simplify", "verify"]
    assert dumps[-1].text.rstrip().endswith("}")
    assert "Machine code" not in dumps[-1].text


def test_build_lane_b_attaches_the_correlation_to_instruction_selection():
    """Machine IR does not exist before ISel, so the lane's first card is the
    one that made it -- and the only one the view belongs on."""
    passes, _, _ = build_lane_b(LLC_STDERR)
    assert [p.pass_id for p in passes] == ["x86-isel", "finalize-isel"]
    corr = passes[0].isel_map["f"]
    assert not passes[1].isel_map
    # The IR paired against is the last dump before ISel -- the one with the
    # dead block loop-simplify's dump does not have.
    assert "dead:" in corr["ir"]
    assert [b["irBlock"] for b in corr["mirBlocks"]] == [0, 1, 2, None]
    # An IR block ISel dropped stays visible on the IR side, unclaimed.
    assert len(corr["irBlocks"]) == 4
