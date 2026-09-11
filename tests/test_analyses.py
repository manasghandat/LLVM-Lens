"""Unit tests for the program-structure analyses (PDT/CDG/DDG/PDG/MDG/CG/LNT)."""

from __future__ import annotations

import re

from llvm_lens.analyses import compute_analyses
from llvm_lens.cfg import ir_cfg


def _edges(dot: str) -> list[tuple[str, str]]:
    """Decode a _render_dot string into (source_name, target_name) edges."""
    names = {int(i): name for i, name in re.findall(r'n(\d+) \[name="([^"]*)"', dot)}
    return [
        (names[int(u)], names[int(v)])
        for u, v in re.findall(r'n(\d+) -> n(\d+);', dot)
    ]


# A function with a loop, a branch, and a call into a second function.
MODULE = '''
define i32 @square(i32 %x) {
entry:
  %1 = mul i32 %x, %x
  ret i32 %1
}

define i32 @sum(i32 %n) {
entry:
  br label %header

header:
  %i = phi i32 [ 0, %entry ], [ %next, %latch ]
  %acc = phi i32 [ 0, %entry ], [ %s2, %latch ]
  %cond = icmp slt i32 %i, %n
  br i1 %cond, label %body, label %exit

body:
  %s2 = add i32 %acc, %i
  br label %latch

latch:
  %next = add i32 %i, 1
  br label %header

exit:
  %r = call i32 @square(i32 %acc)
  ret i32 %acc
}
'''


def test_ir_cfg_exposes_blocks_and_successors():
    entry, blocks = ir_cfg(
        "define i32 @f(i1 %c) {\n"
        "entry:\n"
        "  br i1 %c, label %a, label %b\n"
        "a:\n"
        "  ret i32 1\n"
        "b:\n"
        "  ret i32 2\n"
        "}\n"
    )
    succ = {b.name: b.successors for b in blocks}
    assert entry == "entry"
    assert succ["entry"] == ("a", "b")
    assert succ["a"] == ()
    assert succ["b"] == ()


def test_analyses_payload_shape():
    result = compute_analyses(MODULE)
    assert set(result) == {"callGraph", "functions"}
    assert set(result["functions"]) == {"square", "sum"}
    assert set(result["functions"]["sum"]) == {"pdt", "cdg", "ddg", "pdg", "mdg", "lnt"}
    for fn, graphs in result["functions"].items():
        for dot in graphs.values():
            assert dot.startswith("digraph {")
    assert result["callGraph"].startswith("digraph {")


def test_call_graph_links_caller_to_callee():
    edges = _edges(compute_analyses(MODULE)["callGraph"])
    assert ("sum", "square") in edges
    assert all(e[0] != e[1] for e in edges)  # no spurious self-loops


def test_postdominator_tree_orders_exit_to_entry():
    edges = _edges(compute_analyses(MODULE)["functions"]["sum"]["pdt"])
    # The tree is rooted at the virtual exit: exit -> ... -> entry.
    assert ("__exit__", "exit") in edges
    assert ("exit", "header") in edges
    assert ("header", "entry") in edges
    # Every block except the exit root has exactly one incoming edge.
    targets = [dst for _, dst in edges]
    for name in ("entry", "header", "body", "latch", "exit"):
        assert targets.count(name) == 1


def test_control_dependence_marks_loop_body_not_exit():
    edges = _edges(compute_analyses(MODULE)["functions"]["sum"]["cdg"])
    assert ("header", "body") in edges
    assert ("header", "latch") in edges
    assert ("header", "exit") not in edges


def test_data_dependence_captures_def_use_including_phi():
    edges = _edges(compute_analyses(MODULE)["functions"]["sum"]["ddg"])
    assert ("%i", "%cond") in edges
    assert ("%acc", "%s2") in edges
    assert ("%i", "%next") in edges
    # phi forward reference: %next is defined after %i's phi but still links back.
    assert ("%next", "%i") in edges
    assert ("%s2", "%acc") in edges


def test_program_dependence_is_union_of_control_and_data():
    edges = _edges(compute_analyses(MODULE)["functions"]["sum"]["pdg"])
    assert ("header", "body") in edges      # control
    assert ("header", "latch") in edges     # control
    assert ("header", "exit") in edges      # data: %acc defined in header, used in exit


def test_loop_nest_tree_detects_single_loop():
    edges = _edges(compute_analyses(MODULE)["functions"]["sum"]["lnt"])
    assert ("entry", "header") in edges


def test_memory_dependence_chains_stores_to_same_location():
    ir = (
        "define void @g(ptr %p, ptr %q) {\n"
        "entry:\n"
        "  store i32 1, ptr %p\n"
        "  %v = load i32, ptr %p\n"
        "  store i32 %v, ptr %q\n"
        "  ret void\n"
        "}\n"
    )
    edges = _edges(compute_analyses(ir)["functions"]["g"]["mdg"])
    # store -> load (RAW) and store -> store (WAW) on may-alias pointers.
    assert len(edges) == 2
    assert edges[0][0] == "m0" and edges[1][0] == "m0"
