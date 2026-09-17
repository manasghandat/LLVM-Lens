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
    # open is the one with the longest chain -- the figure's job is to show the
    # list of passes that wrote a line, so the busiest line is the honest one.
    # The chains are read out of the report's own blame model, before any click.
    pick_mode(cdp, "blame")
    fig(cdp, "03-blame.png", note="blame gutter, one tint band per pass run")
    worst = cdp.eval("""(() => {
        const walk = BLAME && BLAME.walk;
        if (!walk) return null;
        let best = null;
        walk.history.forEach((chain, i) => {
            if (chain && (!best || chain.length > best.n)) best = {n: chain.length, i};
        });
        return best;
    })()""")
    if not worst or worst["n"] < 2:
        raise AssertionError(f"no line with a real lineage to show: {worst}")
    clicked = cdp.eval(f"""(() => {{
        const r = [...document.querySelectorAll('#split .urow')][{worst['i']}];
        if (!r) return null;
        r.click();
        return r.textContent.trim().slice(0, 60);
    }})()""")
    cdp.wait(f"document.querySelectorAll('#split .brow').length === {worst['n']}",
             "blame inspector")
    time.sleep(0.8)
    print(f"    clicked row {worst['i'] + 1} ({worst['n']} writers): {clicked!r}")
    print("    chain:", cdp.eval("""[...document.querySelectorAll('#split .brow')]
        .map(x => [x.querySelector('.brun').textContent,
                   x.querySelector('.bname').textContent,
                   x.querySelector('.bkind').textContent].join(' '))"""))
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

    # The analysis graphs are module-wide, so the card in front does not matter.
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

    # The call graph, taken here rather than on json-query: this module has four
    # nodes and fits at a readable zoom, where lexer.cpp's sixty-odd fit to
    # x0.31 and read as a smear. The chip row is part of the figure -- it is the
    # report saying which seven graphs it can draw.
    pick_mode(cdp, "analyses")
    time.sleep(2.0)
    chips = cdp.eval("""[...document.querySelectorAll('#split .ptab[data-analysis]')]
        .map(b => b.dataset.analysis)""")
    print(f"    graphs offered: {chips}")
    cdp.wait("!!document.querySelector('#split .ptab[data-analysis=\"cg\"]')",
             "call graph chip")
    cdp.click('#split .ptab[data-analysis="cg"]', "call graph chip")
    time.sleep(2.5)
    print("    zoom:", cdp.eval('document.querySelector(".cfg-cy-zoom").textContent'))
    fig(cdp, "32-callgraph.png", clip="#split", dsf=2,
        note="the module-wide call graph, and the seven the pane offers")


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


# --- the ask-AI panel, answering a real question ----------------------------

AI_QUESTION = ("What is the significance of this pass in the pipeline, and how "
               "did it change the control-flow graph for this function?")


def askai(cdp):
    """The one figure that needs a credential, and the only non-deterministic
    one: it reads $S/jsonq-ai -- built without --no-ai, so the key sits in that
    report's own sidecar and never in this repository -- and the answer is
    whatever the model returns. The viewport is resized to the transcript once
    it arrives: the panel is a full-height column, so a fixed height leaves a
    band of dead space under a long answer and clips a longer one."""
    open_report(cdp, "jsonq-ai", w=2048, h=820)
    cdp.wait("isConfigured()", "an AI key in the report")
    pick_pass(cdp, find_pass(cdp, "SimplifyCFGPass", changed=True))
    pick_mode(cdp, "diff")
    pick_run(cdp, 43)
    pick_mode(cdp, "cfg")
    pick_function(cdp, find_function(cdp, "5Lexer10lex_numberEv"))
    time.sleep(2.0)

    cdp.click("#aiBtn", "ask AI")
    time.sleep(1.0)
    ctx = cdp.eval("document.getElementById('aiCtx').textContent")
    cdp.eval(f"document.getElementById('aiPrompt').value = {AI_QUESTION!r}; true")
    cdp.click("#aiSend", "send")

    end = time.time() + 180
    while time.time() < end:
        state = cdp.eval("(() => ({pending: AI_PENDING, notice: !!AI_NOTICE,"
                         " msgs: document.querySelectorAll('#aiMessages .ai-msg').length}))()")
        if state["notice"]:
            # This card's pass log is 354 kB, so the panel asks before sending.
            cdp.click('#aiMessages .ai-ask-btn[data-aisend="trimmed"]', "send trimmed")
        elif not state["pending"] and state["msgs"] >= 2:
            break
        time.sleep(2)
    else:
        raise AssertionError("the panel never answered")

    err = cdp.eval("AI_ERROR")
    if err:
        raise AssertionError(f"the panel errored: {err}")
    answer = cdp.eval("""[...document.querySelectorAll('#aiMessages .ai-msg.bot .ai-text')]
        .map(e => e.textContent).join('\\n')""").strip()
    if len(answer) < 200:
        raise AssertionError(f"a suspiciously short answer: {answer!r}")
    print(f"    context : {ctx}")
    print(f"    asked   : {AI_QUESTION}")
    print(f"    answered: {len(answer)} chars -- {answer[:120]}...")

    # Fit the window to the transcript, so the crop is all content and no gap.
    # The transcript box is a stretched flex child: its clientHeight is the room
    # it was given, not the text it holds, so the text is measured off the last
    # message's own bottom edge instead.
    fit = cdp.eval("""(() => {
        const panel = document.getElementById('aiPanel');
        const ms = document.getElementById('aiMessages');
        const last = [...ms.querySelectorAll('.ai-msg')].pop();
        const chrome = panel.getBoundingClientRect().height - ms.clientHeight;
        const pad = parseFloat(getComputedStyle(ms).paddingBottom) || 0;
        return {chrome: Math.ceil(chrome),
                content: Math.ceil(last.getBoundingClientRect().bottom
                                   - ms.getBoundingClientRect().top + pad),
                slack: window.innerHeight - panel.getBoundingClientRect().height};
    })()""")
    h = fit["chrome"] + fit["content"] + fit["slack"] + 2
    cdp.call("Emulation.setDeviceMetricsOverride", width=2048, height=h,
             deviceScaleFactor=2, mobile=False)
    time.sleep(1.5)
    print(f"    window  : {h}px  (chrome {fit['chrome']} + text {fit['content']})")
    fig(cdp, "33-ask-ai.png", clip="#aiPanel", dsf=2,
        note=f"a real answer, {len(answer)} chars, key from the report sidecar")


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
    "askai": askai,
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
