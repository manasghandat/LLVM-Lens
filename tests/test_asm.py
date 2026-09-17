"""Tests for llvm_lens.asm (final machine IR <-> assembly correlation)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from llvm_lens.asm import _mir_kind, asm_function_spans, correlate_asm

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"

MIR = """# Machine code for function f: NoPHIs, TracksLiveness, NoVRegs
Function Live Ins: $edi
bb.0 (%ir-block.1):
  liveins: $edi
  DBG_VALUE $edi, $noreg, !"x", !DIExpression(), debug-location !12
  frame-setup PUSH64r killed $rbp, implicit-def $rsp, implicit $rsp
  frame-setup CFI_INSTRUCTION def_cfa_offset 16
  TEST32rr renamable $edi, renamable $edi, implicit-def $eflags
  JCC_1 %bb.2, 14, implicit $eflags
bb.1..body (align 16):
; predecessors: %bb.0
  $eax = MOV32rr $edi, implicit-def $rax
  KILL $eax, implicit-def $rax
  $rbp = frame-destroy POP64r implicit-def $rsp, implicit $rsp
  RET64 $eax
bb.2.._crit-edge:
; predecessors: %bb.0
  $eax = XOR32rr undef $eax, undef $eax
  RET64 $eax
"""

ASM = """\t.text
\t.globl\tf                       # -- Begin function f
\t.p2align\t4
f:                                      # @f
\t.cfi_startproc
# %bb.0:
\t#DEBUG_VALUE: f:x <- $edi
\tpushq\t%rbp
\t.cfi_def_cfa_offset 16
.Ltmp0:
\t.loc\t0 3 7 prologue_end
\ttestl\t%edi, %edi
\tjle\t.LBB0_2
\t.p2align\t4
# %bb.1:                                # %body
\tmovl\t%edi, %eax
\t# kill: def $eax killed $eax def $rax
\tpopq\t%rbp
\tretq
.LBB0_2:                                # %_crit-edge
\txorl\t%eax, %eax
\tretq
\t.cfi_endproc
                                        # -- End function
\t.globl\tg                       # -- Begin function g
g:
\tretq
                                        # -- End function
"""


def _line(text: str, needle: str) -> int:
    return next(i for i, line in enumerate(text.splitlines()) if needle in line)


def test_asm_function_spans_follow_the_begin_and_end_markers():
    spans = asm_function_spans(ASM)
    assert set(spans) == {"f", "g"}
    assert spans["f"] == (_line(ASM, "Begin function f"), _line(ASM, "\t.cfi_endproc") + 1)


def _named(mir: str, asm: str, pairs) -> list[tuple[str, str]]:
    mir_lines, asm_lines = mir.splitlines(), asm.splitlines()
    return [(_mir_kind(mir_lines[m])[1], asm_lines[a].strip()) for m, a in pairs]


def test_correlate_asm_pairs_blocks_by_number_and_instructions_in_order():
    corr = correlate_asm(MIR, ASM, "f")
    assert [b["name"] for b in corr["blocks"]] == ["bb.0", "bb.1..body", "bb.2.._crit-edge"]
    assert [b["asmStart"] for b in corr["blocks"]] == [
        _line(ASM, "# %bb.0:"), _line(ASM, "# %bb.1:"), _line(ASM, ".LBB0_2:")]
    assert _named(MIR, ASM, corr["pairs"]) == [
        ("PUSH64r", "pushq\t%rbp"),
        ("TEST32rr", "testl\t%edi, %edi"),
        ("JCC_1", "jle\t.LBB0_2"),
        ("MOV32rr", "movl\t%edi, %eax"),
        ("KILL", "# kill: def $eax killed $eax def $rax"),
        ("POP64r", "popq\t%rbp"),
        ("RET64", "retq"),
        ("XOR32rr", "xorl\t%eax, %eax"),
        ("RET64", "retq"),
    ]


def test_correlate_asm_aligns_by_mnemonic_when_counts_differ():
    mir = MIR.replace("  $eax = XOR32rr undef $eax, undef $eax\n",
                      "  $ecx = MOV32rr $edi\n  $eax = XOR32rr undef $eax, undef $eax\n")
    corr = correlate_asm(mir, ASM, "f")
    last_block = [pair for pair in corr["pairs"] if pair[0] > _line(mir, "bb.2..")]
    assert _named(mir, ASM, last_block) == [("XOR32rr", "xorl\t%eax, %eax"), ("RET64", "retq")]


def test_correlate_asm_without_the_function_or_its_blocks_is_none():
    assert correlate_asm(MIR, ASM, "missing") is None
    assert correlate_asm(MIR.replace("bb.", "xx."), ASM, "f") is None


ASM_VIEW_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const start = src.indexOf("function isAsmDebugLine");
const end = src.indexOf("function renderMain()");
if (start < 0 || end < 0) { console.error("asm section not found"); process.exit(2); }
const escapeHtml = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const splitLines = t => (t || "").replace(/\n$/, "").split("\n");
const dropDebugLine = l => /^\s*(DBG_|(frame-(setup|destroy)\s+)?CFI_INSTRUCTION|; predecessors:)/.test(l);
const highlightIR = t => escapeHtml(t);
const pane = (title, chips, stat, body) => `<section data-title="${title}" data-stat="${stat}">${body}</section>`;
let STATE = { fn: "f", orientation: "side", asmSel: null };
let CURRENT_PASS = { asm: data.asm, asmMap: { f: data.corr }, functions: { f: { after: data.mir } } };
const fnChange = fn => CURRENT_PASS.functions[fn];
eval(src.slice(start, end));

const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };
const html = asmPaneHtml();
check("pairs machine ir with assembly", html.includes("asmmir") && html.includes("asmasm"));
check("stat counts pairs and blocks", html.includes('data-stat="9 instructions paired · 3/3 blocks · f"'));
check("debug and cfi lines hidden on both sides",
  !html.includes("DBG_VALUE") && !html.includes("CFI_INSTRUCTION")
  && !html.includes("#DEBUG_VALUE") && !html.includes(".cfi_") && !html.includes(".loc") && !html.includes(".Ltmp"));
check("only the selected function's assembly", !html.includes("# -- Begin function g"));
const [m, a] = data.corr.pairs[0];
check("a paired machine row names its line", html.includes(`class="urow amap" data-amir="${m}"`));
check("a paired asm row names its line", html.includes(`class="urow amap" data-aasm="${a}"`));
check("directives stay, dimmed", /class="urow anone" data-aasm="\d+"[^>]*><span class="uln">\d+<\/span><code class="utext">\t\.p2align/.test(html));

CURRENT_PASS = { asm: data.asm, asmMap: {}, functions: { f: { after: data.mir } } };
const plain = asmPaneHtml();
check("no correlation falls back to the whole file", plain.includes("data-asmln") && plain.includes("Begin function g"));
if (failures.length) { console.error("FAIL: " + failures.join(", ")); process.exit(1); }
console.log("asm view checks passed");
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_asm_view_renders_the_pair(tmp_path):
    payload = tmp_path / "asm.json"
    payload.write_text(json.dumps({"asm": ASM, "mir": MIR, "corr": correlate_asm(MIR, ASM, "f")}))
    result = subprocess.run(
        ["node", "-e", ASM_VIEW_HARNESS, str(FRONTEND / "app.js"), str(payload)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "asm view checks passed" in result.stdout
