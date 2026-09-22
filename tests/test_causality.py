"""Pass causality: the tracker's log, the ablation graph, and its alignment."""

from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace

import pytest

from llvm_lens.causality import (
    ENABLES, PREEMPTS, Trace, align_ords, analyze, build_graph, build_tracker,
    causality_document, chains, parse_trace, run_trace,
)
from llvm_lens.parsers.debug_pass_manager import parse_pass_runs

# --- a synthetic pipeline: a -> b -> c, with a pre-empting d ------------------

RUN = "PROV-RUN\t{o}\t{k}\t{c}\t{n}\tfunction @f"
CHANGE = "PROV\t{o}\t{k}\tf\tchanged\t00000000000000{o:02d}"


def _log(*changed: tuple[int, str, str], runs=(), skipped=()) -> str:
    lines = []
    for ord_, key, cls in runs:
        lines.append(RUN.format(o=ord_, k=key, c=cls, n=cls.lower()))
        if (ord_, key, cls) in changed:
            lines.append(CHANGE.format(o=ord_, k=key))
    for key in skipped:
        lines.append(f"PROV-SKIP\t{key}\tX\tfunction @f\tablated")
    return "\n".join(lines)


RUNS = [
    (1, "f/APass#1", "APass"),
    (2, "f/BPass#1", "BPass"),
    (3, "f/CPass#1", "CPass"),
    (4, "f/DPass#1", "DPass"),
]
A, B, C, D = RUNS


def _trace(changed, skip=None, digest="final"):
    runs = [r for r in RUNS if r[1] != skip]
    trace = parse_trace(_log(*changed, runs=runs, skipped=[skip] if skip else []))
    trace.final_digest = digest
    return trace


@pytest.fixture
def graph():
    baseline = _trace([A, B, C])
    ablations = {
        # Without a, neither b nor c fires: a enables both, c only through b.
        A[1]: _trace([], skip=A[1], digest="other"),
        # Without b, c does not fire either.
        B[1]: _trace([A], skip=B[1], digest="other"),
        # Without c, d does c's work: c pre-empts d, and the result is the same.
        C[1]: _trace([A, B, D], skip=C[1]),
    }
    return build_graph(baseline, ablations)


def test_parse_trace_reads_every_record():
    trace = parse_trace(_log(A, runs=[A, B], skipped=[B[1]]) + "\nnoise\tline")
    assert [r.key for r in trace.runs] == [A[1], B[1]]
    assert trace.runs[0].cls == "APass" and trace.runs[0].unit == "function @f"
    assert [(c.ord, c.key, c.entity) for c in trace.changes] == [(1, A[1], "f")]
    assert trace.skipped == [(B[1], "ablated")]
    assert trace.effects() == {A[1]: {"f"}}


def test_enabling_edges_come_from_what_stops_firing(graph):
    enables = {(e.cause, e.effect): e for e in graph.edges if e.kind == ENABLES}
    assert set(enables) == {(A[1], B[1]), (A[1], C[1]), (B[1], C[1])}
    assert enables[(A[1], B[1])].entities == ("f",)


def test_an_edge_a_longer_chain_explains_is_not_direct(graph):
    direct = {(e.cause, e.effect): e.direct for e in graph.edges if e.kind == ENABLES}
    assert direct[(A[1], B[1])] and direct[(B[1], C[1])]
    assert not direct[(A[1], C[1])]


def test_pre_empting_edges_come_from_what_starts_firing(graph):
    pre = [(e.cause, e.effect) for e in graph.edges if e.kind == PREEMPTS]
    assert pre == [(C[1], D[1])]


def test_redundant_invocations_leave_the_final_ir_alone(graph):
    assert graph.nodes[A[1]].final_differs is True
    assert graph.nodes[C[1]].final_differs is False
    assert graph.nodes[D[1]].final_differs is None  # changed nothing; not ablated


def test_an_ablation_that_did_not_skip_is_an_error_not_evidence():
    baseline = _trace([A, B])
    unskipped = _trace([A], skip=None)  # no PROV-SKIP for A's key
    graph = build_graph(baseline, {A[1]: unskipped})
    assert graph.nodes[A[1]].error == "the skip did not take effect"
    assert graph.edges == []


def test_chains_follow_direct_edges(graph):
    assert chains(graph) == [[A[1], B[1], C[1]]]


def test_alignment_skips_names_the_lane_a_parser_drops(graph):
    # Lane A's parser keeps only pass names without spaces.
    graph.nodes["[module]/Req<a, b>#1"] = SimpleNamespace(
        cls="Req<a, b>", ords=[2], in_baseline=True)
    for key, ord_ in ((B[1], 3), (C[1], 4), (D[1], 5)):
        graph.nodes[key].ords = [ord_]
    runs = [SimpleNamespace(name=name, index=i)
            for i, name in enumerate(["APass", "BPass", "CPass", "DPass"], start=1)]
    assert align_ords(graph, runs) == {1: 1, 3: 2, 4: 3, 5: 4}


def test_alignment_refuses_a_different_pipeline(graph):
    runs = [SimpleNamespace(name=name, index=i)
            for i, name in enumerate(["APass", "ZPass", "CPass", "DPass"], start=1)]
    assert align_ords(graph, runs) == {}


def test_document_indexes_edges_by_node(graph):
    doc = causality_document(graph, {1: 11, 2: 12, 3: 13, 4: 14})
    keys = [n["key"] for n in doc["nodes"]]
    assert keys == [A[1], B[1], C[1], D[1]]
    assert doc["nodes"][0]["changedRuns"] == [11]
    edge = next(e for e in doc["edges"] if e["kind"] == PREEMPTS)
    assert (keys[edge["from"]], keys[edge["to"]]) == (C[1], D[1])
    assert doc["aligned"] and not doc["truncated"]


# --- the real tracker ---------------------------------------------------------


@pytest.fixture(scope="session")
def tracker(toolchain, tmp_path_factory):
    from llvm_lens.causality import CausalityError
    try:
        return build_tracker(toolchain, cache_dir=tmp_path_factory.mktemp("prov"))
    except CausalityError as exc:
        pytest.skip(str(exc))


def test_tracker_agrees_with_print_changed(toolchain, tracker, sample_ir):
    """Same invocations, same numbering, and the same passes changed the IR."""
    err = subprocess.run(
        [str(toolchain.opt.path), f"-load-pass-plugin={tracker}", "-passes=default<O2>",
         "-print-changed=quiet", "-debug-pass-manager", str(sample_ir), "-o", "/dev/null"],
        capture_output=True, text=True, check=True).stderr
    running: list[str] = []
    dumped: set[int] = set()
    for line in err.splitlines():
        match = re.match(r"^Running pass: (.+?) on ", line)
        if match:
            running.append(match.group(1))
        elif line.startswith("*** IR Dump After "):
            dumped.add(len(running))
    trace = parse_trace(err)
    assert [r.cls for r in trace.runs] == running
    changed = {c.ord for c in trace.changes}
    assert dumped <= changed
    # The one thing -print-changed cannot see: a loop pass that deleted its
    # loop (a full unroll) is reported as invalidated, and never dumped.
    extra = {trace.runs[o - 1].cls for o in changed - dumped}
    assert extra <= {"LoopFullUnrollPass"}


def test_prov_skip_ablates_exactly_one_invocation(toolchain, tracker, sample_ir):
    baseline = run_trace(toolchain, tracker, sample_ir, "default<O2>")
    key = next(c.key for c in baseline.changes if "SimplifyCFG" in c.key)
    ablated = run_trace(toolchain, tracker, sample_ir, "default<O2>", skip=[key])
    assert ablated.skipped == [(key, "ablated")]
    assert key not in ablated.effects()
    before = [r.key for r in baseline.runs]
    assert [r.key for r in ablated.runs][:before.index(key)] == before[:before.index(key)]


def test_analyze_lines_up_with_lane_a(toolchain, tracker, sample_ir, tmp_path):
    graph = analyze(toolchain, sample_ir, "default<O2>", tracker=tracker, limit=6)
    assert graph.ablated == 6 and graph.truncated
    stderr = subprocess.run(
        [str(toolchain.opt.path), "-passes=default<O2>", "-debug-pass-manager",
         str(sample_ir), "-o", "/dev/null"], capture_output=True, text=True).stderr
    # No -S here: lane A ends on the bitcode writer, the tracker on the printer.
    runs = parse_pass_runs(stderr)
    run_of = align_ords(graph, runs)
    assert run_of, "the traced pipeline must map onto lane A's runs"
    names = {r.index: r.name for r in runs}
    for node in graph.nodes.values():
        for ord_ in node.ords:
            if ord_ in run_of:
                assert names[run_of[ord_]] == node.cls
