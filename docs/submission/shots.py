"""Capture the submission's figures.

Run `python3 -m http.server 8765` from the directory holding the built reports
(see build-reports.sh), then `python3 shots.py [figure-name ...]`.

Every figure asserts the state it captured, so a view that silently failed to
render raises instead of shipping a blank pane.
"""

import sys
import time

from capture import (OUT, assert_ctx, block_counts, find_function,
                     find_pass, find_pass_matching, launch, open_report,
                     pick_function, pick_lane, pick_mode, pick_pass, pick_run,
                     pick_tab, source_pane)

DONE = []


def fig(cdp, name, clip=None, dsf=2, note=""):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    rect = cdp.clip_of(clip) if clip else None
    raw = cdp.shot(path, dsf=dsf, clip=rect)
    DONE.append(name)
    size = ""
    try:
        from PIL import Image
        size = " %dx%d" % Image.open(path).size
    except Exception:
        pass
    print(f"  {name:<28}{len(raw) // 1024:>6} KB{size}   {note}")


# --- the json-query report, one translation unit ----------------------------

def jsonq(cdp):
    open_report(cdp, "jsonq")

    # The workspace: rail, spine, pass list, split panes, drawer, strip.
    pick_pass(cdp, find_pass(cdp, "SROAPass", changed=True))
    pick_mode(cdp, "diff")
    # SROA ran 28 times and only its 9th run touched this function, so the run
    # is picked: the card opens on its last, where there is nothing to diff.
    pick_run(cdp, 9)
    pick_function(cdp, find_function(cdp, "5Lexer7advanceEv"))
    assert_ctx(cdp, "SROAPass")
    fig(cdp, "01-workspace.png", note="the workstation, SROA run 9 on Lexer::advance")
    fig(cdp, "02-diff-sroa.png", clip="#split",
        note="the diff itself: alloca/store/load gone")

    # Blame: the run-ordinal gutter, then a line's whole lineage. The row to
    # click is one a pass actually wrote, not one still reading `in`.
    pick_mode(cdp, "blame")
    fig(cdp, "03-blame.png", note="blame gutter, one tint band per pass run")
    clicked = cdp.eval("""(() => {
        const rows = [...document.querySelectorAll('#split .urow')];
        const r = rows.find(x => {
            const b = x.querySelector('.ublame');
            return b && b.textContent.trim() !== 'in';
        });
        if (!r) return null;
        r.click();
        return r.textContent.slice(0, 60);
    })()""")
    cdp.wait("!!document.querySelector('#split .brow')", "blame inspector")
    time.sleep(0.8)
    print(f"    clicked row: {clicked!r}")
    fig(cdp, "04-blame.png", note="the same pane with a line's lineage open")
    fig(cdp, "05-blame-chain.png", clip="#split", note="the chain, oldest first")

    # Blame reachable from the diff, where the question is actually asked.
    pick_mode(cdp, "diff")
    cdp.eval("""(() => {
        const r = [...document.querySelectorAll('#split .urow.del')][0];
        if (r) r.click();
        return !!r;
    })()""")
    cdp.wait("!!document.querySelector('#split .brow')", "diff inspector")
    time.sleep(0.8)
    fig(cdp, "06-diff-blame.png", clip="#split",
        note="a deleted line, read as it stood before this pass")

    # Flow: the whole lane's shape at once.
    pick_mode(cdp, "pipeline")
    cdp.click('#split .ptab[data-pipe="lane"]', "IR lane only")
    time.sleep(2.5)
    fig(cdp, "07-flow-ir.png", note="flow, IR lane")
    cdp.click('#split .ptab[data-pipe="both"]', "both lanes")
    time.sleep(2.5)
    fig(cdp, "08-flow-both.png", note="flow, both lanes and the opt->llc handoff")
    fig(cdp, "09-flow-detail.png", clip="#split",
        note="the serpentine up close")

    pick_mode(cdp, "structure")
    time.sleep(1.2)
    fig(cdp, "10-structure.png", note="pass-manager hierarchy")

    # CFG on the run that actually reshapes one: SimplifyCFG's 43rd run folds
    # Lexer::lex_number from 18 blocks to 10. A card is one pass however many
    # times it ran, so the run is picked rather than the card.
    lexnum = find_function(cdp, "5Lexer10lex_numberEv")
    pick_pass(cdp, find_pass(cdp, "SimplifyCFGPass", changed=True))
    pick_mode(cdp, "diff")  # the run picker lives in the diff pane
    pick_run(cdp, 43)
    pick_mode(cdp, "cfg")
    pick_function(cdp, lexnum)
    time.sleep(2.5)
    assert_ctx(cdp, "lex_numberEv")
    blocks = block_counts(cdp, lexnum)
    if not blocks or blocks["after"] >= blocks["before"]:
        raise AssertionError(f"expected this run to shrink lex_number, got {blocks}")
    print(f"    blocks: {blocks['before']} -> {blocks['after']}")
    fig(cdp, "11-cfg.png", note=f"CFG after, Lexer::lex_number ({blocks['after']} blocks)")
    cdp.click('#split .ptab[data-src="both"]', "both chip")
    time.sleep(3.0)
    fig(cdp, "12-cfg-both.png",
        note=f"before ({blocks['before']}) and after ({blocks['after']}) side by side")

    # Analysis graphs, on a function small enough that the dominance tree is
    # readable at the zoom the pane fits it to.
    pick_mode(cdp, "analyses")
    time.sleep(2.0)
    fig(cdp, "13-graphs.png", note="dominance tree, final state")

    # The DDG is per-instruction, so it only stays readable on a small
    # function: Lexer::advance is the same one the SROA card opened on.
    cdp.wait("!!document.querySelector('#split .ptab[data-analysis=\"ddg\"]')",
             "graph chips")
    pick_function(cdp, find_function(cdp, "5Lexer7advanceEv"))
    cdp.click('#split .ptab[data-analysis="ddg"]', "ddg chip")
    time.sleep(2.5)
    assert_ctx(cdp, "advanceEv")
    fig(cdp, "13b-ddg.png", note="data-dependence graph, Lexer::advance")

    # Source correlation. A snapshot only maps back to C++ while its dump still
    # carries the debug metadata, so this wants an early run and a function with
    # real source under it -- late runs render an empty pane. The figure is the
    # file switcher, which only exists when the snapshot spans several files, so
    # the run is one whose inlining pulled json.h into lexer.cpp.
    pick_pass(cdp, find_pass(cdp, "SimplifyCFGPass", changed=True))
    pick_mode(cdp, "diff")
    pick_run(cdp, 18)
    pick_mode(cdp, "src")
    pick_function(cdp, find_function(cdp, "5Lexer4nextEv"))
    time.sleep(2.5)
    src = source_pane(cdp)
    if not src["mapped"]:
        raise AssertionError("source pane has no mapped lines -- an empty figure")
    if len(src["chips"]) < 2:
        raise AssertionError(f"no file switcher to show: {src['chips']}")
    print(f"    source: {src['stat']}  chips={src['chips']}")
    fig(cdp, "14-source.png", note=f"IR line -> C++ line ({src['stat']})")

    # ISel and Asm live at the head and tail of the machine lane.
    pick_lane(cdp, "mir")
    pick_pass(cdp, find_pass_matching(cdp, "Instruction Selection", lane="mir"))
    pick_mode(cdp, "isel")
    pick_function(cdp, find_function(cdp, "5Lexer7advanceEv"))
    time.sleep(2.0)
    fig(cdp, "15-isel.png", note="IR -> machine IR at selection")

    pick_pass(cdp, find_pass_matching(cdp, "Assembly Printer", lane="mir"))
    pick_mode(cdp, "asm")
    time.sleep(2.0)
    fig(cdp, "16-asm.png", note="machine IR -> assembly")

    # The report can say how it was made.
    cdp.click("#cmdBtn", "commands")
    time.sleep(1.0)
    fig(cdp, "17-commands.png", note="the exact clang/opt/llc argv")
    cdp.click("#cmdClose", "close")
    time.sleep(0.5)

    cdp.click("#aiBtn", "ask AI")
    time.sleep(1.0)
    fig(cdp, "18-ask-ai.png", note="ask AI, no key configured")
    cdp.click("#aiClose", "close")
    time.sleep(0.5)


# --- the same program, all seven translation units --------------------------

def linked(cdp):
    open_report(cdp, "jsonq-linked")
    n = cdp.eval("CURRENT_MANIFEST.passes.length")
    src = cdp.eval("CURRENT_MANIFEST.metadata.source")
    print(f"    {n} cards from {src}")
    pick_mode(cdp, "pipeline")
    time.sleep(2.5)
    fig(cdp, "19-linked-flow.png", note=f"whole-program flow, {n} cards")
    pick_mode(cdp, "structure")
    time.sleep(1.5)
    fig(cdp, "20-linked-structure.png", note="whole-program structure")


# --- register pressure: where the spills come from --------------------------

def regpressure(cdp):
    open_report(cdp, "regpressure")
    ra = find_pass_matching(cdp, "Greedy Register Allocator", lane="mir")
    pick_lane(cdp, "mir")
    pick_pass(cdp, ra)
    pick_function(cdp, "mix16")
    pick_tab(cdp, "Spills")
    time.sleep(1.0)
    fig(cdp, "21-spills.png", note="34 spills and reloads for mix16")
    fig(cdp, "22-spills-table.png", clip="#bottom", note="the spill table")

    pick_pass(cdp, find_pass_matching(cdp, "Virtual Register Rewriter", lane="mir"))
    pick_tab(cdp, "RegMap")
    time.sleep(1.0)
    fig(cdp, "23-regmap.png", note="virtual -> physical registers")

    # The contrast the sample is built around.
    pick_function(cdp, "mix4")
    time.sleep(1.2)
    fig(cdp, "24-regmap-mix4.png", note="mix4 fits in 15 registers and spills nothing")


# --- the custom pass --------------------------------------------------------

def mba(cdp):
    open_report(cdp, "mba")
    pid = find_pass(cdp, "MBAAdd", lane="ir")
    pick_pass(cdp, pid)
    pick_mode(cdp, "diff")
    # The card opens on its last run, and that one leaves `sum` alone -- each of
    # its three runs rewrote a different function, so the run is picked.
    pick_run(cdp, 360)
    pick_function(cdp, "sum")
    time.sleep(1.5)
    assert_ctx(cdp, "MBAAdd")
    fig(cdp, "25-custom-diff.png", note="a custom pass turning one add into nine")

    pick_mode(cdp, "pipeline")
    time.sleep(2.0)
    fig(cdp, "26-custom-flow.png", note="the custom pass in the flow")

    pick_mode(cdp, "structure")
    time.sleep(1.2)
    fig(cdp, "27-custom-structure.png", note="custom badge in the hierarchy")


# --- the samples written to make one behaviour legible ----------------------

def vectorize(cdp):
    open_report(cdp, "vectorize")
    pid = find_pass_matching(cdp, "LoopVectorize", lane="ir", changed=True)
    pick_pass(cdp, pid)
    pick_mode(cdp, "diff")
    # It ran twice; only the first widened anything, so the run is picked.
    pick_run(cdp, 241)
    pick_function(cdp, find_function(cdp, "scale_add"))
    time.sleep(1.5)
    fig(cdp, "28-vectorize.png", note="a loop that widened")
    pick_function(cdp, find_function(cdp, "prefix_sum"))
    time.sleep(1.5)
    fig(cdp, "29-vectorize-scalar.png", note="a loop that could not")


def switchlower(cdp):
    open_report(cdp, "switch")
    pick_lane(cdp, "mir")
    pick_pass(cdp, find_pass_matching(cdp, "Instruction Selection", lane="mir"))
    pick_mode(cdp, "diff")
    pick_function(cdp, "region_of")
    time.sleep(1.5)
    fig(cdp, "30-switch.png", note="a dense switch becoming a jump table")


def licm(cdp):
    open_report(cdp, "licm")
    pid = find_pass_matching(cdp, "LICM", lane="ir", changed=True)
    pick_pass(cdp, pid)
    pick_mode(cdp, "diff")
    pick_function(cdp, find_function(cdp, "weighted_sum"))
    time.sleep(1.5)
    fig(cdp, "31-licm.png", note="the invariant multiply, lifted out")


GROUPS = {
    "jsonq": jsonq,
    "linked": linked,
    "regpressure": regpressure,
    "mba": mba,
    "vectorize": vectorize,
    "switch": switchlower,
    "licm": licm,
}


def main():
    want = sys.argv[1:] or list(GROUPS)
    proc, _profile, cdp = launch()
    try:
        for group in want:
            print(f"\n== {group}")
            GROUPS[group](cdp)
    finally:
        proc.terminate()
    print(f"\n{len(DONE)} figures in {OUT}")


if __name__ == "__main__":
    main()
