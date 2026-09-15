"""The lineage walk: which pass put each line where it is."""

from __future__ import annotations

import json

from llvm_lens.blame import (
    INPUT_NAME,
    Entry,
    Timeline,
    advance,
    blame_document,
    blame_key,
    canonical_fn,
    dump_states,
    dumps_by_run,
    entity_text,
    lane_a_slots,
    lane_a_timeline,
    mir_timeline,
    split_lines,
    walk,
)
from llvm_lens.diff import FnChange
from llvm_lens.parsers.debug_pass_manager import PassRun
from llvm_lens.parsers.print_changed import IrSnapshot


def run(name: str, index: int, line: int, function: str = "main") -> PassRun:
    return PassRun(name, function, index, line)


def dump(pass_name: str, function: str, ir: str, line: int, module_scope: bool = False):
    return IrSnapshot(pass_name, function, ir, "", module_scope, line)


MODULE = """\
define i32 @main() {
  %1 = add i32 1, 2
  ret i32 %1
}

define i32 @helper() {
  ret i32 7
}"""


# --- naming -------------------------------------------------------------------


def test_a_single_function_scc_is_named_for_that_function():
    assert canonical_fn("(main)") == "main"


def test_a_multi_function_scc_keeps_its_own_name():
    """Its members get their own rows from the dump body, not from the header."""
    assert canonical_fn("(main, helper)") == "(main, helper)"


def test_a_loop_dump_is_named_for_its_function():
    assert canonical_fn("loop %1 in function main") == "main"


def test_a_plain_entity_is_itself():
    assert canonical_fn("main") == "main"
    assert canonical_fn("[module]") == "[module]"


def test_an_scc_body_is_the_members_bodies():
    text = entity_text("(main, helper)", MODULE)
    assert "add i32 1, 2" in text
    assert "ret i32 7" in text


def test_a_function_body_is_just_that_function():
    assert entity_text("helper", MODULE) == "define i32 @helper() {\n  ret i32 7\n}"


# --- what a dump is authoritative about ---------------------------------------


def test_a_module_dump_fixes_every_function_at_once():
    """-print-module-scope bodies are whole modules whoever the header names."""
    states = dump_states(dump("InstCombinePass", "main", MODULE, 4, True))
    assert set(states) == {"[module]", "main", "helper"}
    assert states["helper"] == "define i32 @helper() {\n  ret i32 7\n}"


def test_a_bare_dump_fixes_only_its_own_entity():
    states = dump_states(dump("SROAPass", "main", "define i32 @main() {\n  ret i32 0\n}", 4))
    assert list(states) == ["main"]


# --- splitting ----------------------------------------------------------------


def test_lines_drop_one_trailing_newline():
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\nb") == ["a", "b"]
    assert split_lines("") == []


# --- the diff plan ------------------------------------------------------------


def test_ssa_renumbering_is_the_same_line():
    assert blame_key("  %3 = add i32 %1, %2") == blame_key("  %7 = add i32 %4, %5")
    assert blame_key("3:") == blame_key("12:")


# --- one pass -----------------------------------------------------------------


def test_a_replaced_line_is_rewritten():
    lines, history = advance(["a", "b", "c"], [[], [], []], 5, "GVNPass", "a\nB\nc")
    assert lines == ["a", "B", "c"]
    assert history[0] == [] and history[2] == []
    assert history[1] == [Entry(5, "GVNPass", "rewritten")]


def test_a_renumbered_line_is_renamed_not_rewritten():
    _lines, history = advance(
        ["  %3 = add i32 %1, %2"], [[]], 3, "Mem2RegPass", "  %4 = add i32 %1, %2",
    )
    assert history[0][0].kind == "renamed"


def test_an_inserted_line_is_created():
    lines, history = advance(["a"], [[]], 5, "InlinerPass", "a\nnew")
    assert lines == ["a", "new"]
    assert history[1] == [Entry(5, "InlinerPass", "created")]


def test_a_deleted_line_leaves_nothing_behind():
    lines, history = advance(["a", "b"], [[], []], 5, "DCEPass", "a")
    assert lines == ["a"]
    assert history == [[]]


def test_a_chain_accumulates_oldest_first():
    lines, history = advance(["a"], [[]], 5, "Mem2RegPass", "a\nx")
    _lines, history = advance(lines, history, 9, "GVNPass", "a\nX")
    assert [e.name for e in history[1]] == ["Mem2RegPass", "GVNPass"]
    assert history[1][0].kind == "created" and history[1][1].kind == "rewritten"


def test_an_untouched_line_keeps_its_chain():
    first_lines, first_history = advance(["a\nb"], [[], []], 5, "A", "a\nB")
    lines, history = advance(first_lines, first_history, 9, "B", "a\nB")
    assert history == first_history and lines == first_lines


# --- the walk -----------------------------------------------------------------


def test_the_first_state_of_a_lane_is_its_input():
    states = walk([(0, INPUT_NAME, "x\ny"), (4, "A", "x\nY")])
    assert states[0].history == [[], []]
    assert states[1].history[1][0].run == 4


def test_a_lane_that_starts_at_a_pass_creates_every_line():
    states = walk([(3, "ISel", "a\nb")])
    assert [e.kind for e in states[0].history[0]] == ["created"]
    assert states[0].history[0][0].run == 3


# --- matching dumps to runs ---------------------------------------------------


def test_a_dump_belongs_to_the_last_run_of_that_name_before_it():
    runs = [run("A", 1, 10), run("B", 2, 20), run("A", 3, 30)]
    dumps = [dump("A", "main", "x", 15), dump("A", "main", "y", 35)]
    found = dumps_by_run(runs, dumps)
    assert [d.ir for d in found[1]] == ["x"]
    assert [d.ir for d in found[3]] == ["y"]
    assert 2 not in found


def test_a_dump_with_no_run_before_it_is_dropped():
    assert dumps_by_run([run("A", 1, 10)], [dump("A", "main", "x", 5)]) == {}


# --- the card list ------------------------------------------------------------


def test_every_run_that_dumped_gets_its_own_card():
    """A pass that ran ten times and changed ten times gets ten cards."""
    runs = [run("A", 1, 10), run("B", 2, 20), run("A", 3, 30)]
    dumps = [dump("A", "main", "x", 15), dump("A", "main", "y", 35)]
    slots = lane_a_slots(runs, dumps)
    assert [(s.name, s.run.index, [d.ir for d in s.dumps]) for s in slots] == [
        ("A", 1, ["x"]), ("B", 2, []), ("A", 3, ["y"]),
    ]


def test_a_pass_that_never_changed_anything_is_one_card():
    runs = [run("A", 1, 10), run("A", 3, 30), run("B", 2, 20), run("B", 4, 40)]
    slots = lane_a_slots(runs, [dump("A", "main", "x", 15)])
    assert [(s.name, s.run.index) for s in slots] == [("A", 1), ("B", 2)]


def test_the_driver_passes_get_no_card():
    runs = [run("VerifierPass", 1, 10), run("PrintModulePass", 2, 20), run("A", 3, 30)]
    slots = lane_a_slots(runs, [dump("A", "main", "x", 35)])
    assert [s.name for s in slots] == ["A"]


def test_card_positions_are_their_own_numbering():
    """Cards are numbered by position, not by the pass-manager run index."""
    runs = [run("A", 7, 10), run("B", 9, 20)]
    slots = lane_a_slots(runs, [dump("A", "main", "x", 15)])
    assert [s.run_index for s in slots] == [1, 2]


# --- the timeline -------------------------------------------------------------


def test_a_repeated_state_is_not_recorded_twice():
    timeline = Timeline()
    assert timeline.record("main", 1, "A", "x") is True
    assert timeline.record("main", 2, "A", "x") is False
    assert timeline.states["main"] == [(1, "A", "x")]


def test_the_state_at_a_run_is_the_last_one_at_or_before_it():
    timeline = Timeline()
    timeline.record("main", 1, "A", "x")
    timeline.record("main", 5, "B", "y")
    assert timeline.at("main", 0) == ""
    assert timeline.at("main", 1) == "x"
    assert timeline.at("main", 4) == "x"
    assert timeline.at("main", 9) == "y"


def test_the_seed_is_the_input_state_of_every_function():
    timeline = Timeline()
    timeline.seed(MODULE)
    assert timeline.at("main", 0) == "define i32 @main() {\n  %1 = add i32 1, 2\n  ret i32 %1\n}"
    assert timeline.states["main"][0][2] and timeline.states["main"][0][1] == INPUT_NAME


def test_only_the_runs_that_changed_something_are_listed():
    timeline = Timeline()
    timeline.record("main", 1, "A", "x")
    timeline.record("helper", 1, "A", "y")
    timeline.record("main", 2, "B", "x")
    assert timeline.changed_at(1) == ["main", "helper"]
    assert timeline.changed_at(2) == []


def test_lane_a_follows_the_stream_run_by_run():
    runs = [run("A", 1, 10), run("A", 2, 20)]
    body = "define i32 @main() {\n  ret i32 0\n}"
    later = "define i32 @main() {\n  ret i32 0, !dbg !1\n}"
    slots = lane_a_slots(
        runs, [dump("A", "main", body, 15, True), dump("A", "main", later, 25, True)],
    )
    timeline = lane_a_timeline(slots, body)
    # The first dump left main exactly as the input had it, so it is no state.
    assert [entry[0] for entry in timeline.states["main"]] == [0, 2]
    assert timeline.at("main", 2) == later
    assert timeline.at("main", 1) == body


# --- the machine lane ---------------------------------------------------------


class _Card:
    """A machine-lane card: one pass, and the state its diff lands on."""

    def __init__(self, run_index: int, name: str, functions: dict[str, str]) -> None:
        self.run_index = run_index
        self.name = name
        self.functions = {fn: FnChange(fn, "", text) for fn, text in functions.items()}


def test_the_machine_lane_follows_the_cards():
    cards = [_Card(1, "ISel", {"main": "body"}), _Card(2, "RegAlloc", {"main": "body2"})]
    timeline = mir_timeline(cards)
    assert timeline.states["main"] == [(1, "ISel", "body"), (2, "RegAlloc", "body2")]


def test_a_card_that_left_the_function_alone_is_no_state():
    """Otherwise blame could name a state the card's own diff does not show."""
    cards = [_Card(1, "ISel", {"main": "body"}), _Card(2, "Nop", {"main": "body"})]
    assert len(mir_timeline(cards).states["main"]) == 1


# --- the document the report ships --------------------------------------------


def test_the_document_interns_names_and_chains():
    timeline = Timeline()
    timeline.seed("define i32 @main() {\n  ret i32 0\n}")
    timeline.record("main", 5, "GVNPass", "define i32 @main() {\n  ret i32 1\n}")
    doc = blame_document("ir", timeline)
    assert doc["lane"] == "ir"
    assert doc["names"] == [INPUT_NAME, "GVNPass"]
    assert doc["kinds"] == ["created", "rewritten", "renamed"]
    assert doc["events"] == [[5, 1, 1]]
    assert doc["hist"] == [[], [0]]
    assert doc["functions"]["main"]["text"] == [
        "define i32 @main() {", "  ret i32 1", "}",
    ]
    assert doc["functions"]["main"]["states"] == [
        {"run": 0, "h": [0, 0, 0]}, {"run": 5, "h": [0, 1, 0]},
    ]


def test_the_document_shares_one_chain_id_between_equal_lines():
    """Two lines the same pass wrote the same way are one chain, stored once."""
    timeline = Timeline()
    timeline.seed("define i32 @main() {\n  ret i32 0\n  ret i32 0\n}")
    timeline.record("main", 5, "A", "define i32 @main() {\n  ret i32 1\n  ret i32 1\n}")
    doc = blame_document("ir", timeline)
    assert doc["functions"]["main"]["states"][-1]["h"] == [0, 1, 1, 0]
    assert doc["hist"][1] == [0]


def test_the_document_is_json_serialisable():
    timeline = Timeline()
    timeline.seed(MODULE)
    timeline.record("main", 1, "A", MODULE.replace("1, 2", "1, 3"))
    text = json.dumps(blame_document("ir", timeline))
    assert json.loads(text)["functions"]["main"]["text"][1].strip() == "%1 = add i32 1, 3"
