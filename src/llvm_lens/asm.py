"""Correlate the final machine IR against the assembly llc printed from it.

Observed LLVM 22 x86 output (llc's default -asm-verbose):

    	.globl	main                    # -- Begin function main
    main:                                   # @main
    # %bb.0:                                ; an unlabelled block
    	pushq	%rbp
    .LBB0_1:                                ; a block something jumps to
    	.loc	0 20 5                          # debug info, no MIR line
    	#DEBUG_VALUE: main:a <- $rdi            # a DBG_VALUE
    	# kill: def $eax killed $eax def $rax  # a KILL
    	retq
    	.cfi_endproc
                                            # -- End function

Machine block ``bb.N`` is printed as ``# %bb.N:`` or ``.LBB<f>_N:``, and its
instructions come out in order, one line each.
"""

from __future__ import annotations

import re

from .isel import mir_block_spans

BEGIN_FN_RE = re.compile(r"# -- Begin function (\S+)\s*$")
END_FN_RE = re.compile(r"^\s*# -- End function\s*$")
ASM_BLOCK_RE = re.compile(r"^(?:# %bb\.(\d+):|\.LBB\d+_(\d+):)")
MIR_BLOCK_NUM_RE = re.compile(r"^bb\.(\d+)")
ASM_INSN_RE = re.compile(r"^\t([a-z][\w.]*)(?:\t|\s|$)")
ASM_KILL_RE = re.compile(r"^\t# kill:")
MIR_OPCODE_RE = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
MIR_SKIP_RE = re.compile(r"^\s*(?:DBG_|(?:frame-(?:setup|destroy)\s+)?CFI_INSTRUCTION|EH_LABEL|; |liveins:|successors:)")
STEM_ALIASES = {"jcc": "j", "setcc": "set", "cmovcc": "cmov", "tailjmp": "jmp", "noop": "nop"}


def asm_function_spans(asm_text: str) -> dict[str, tuple[int, int]]:
    spans: dict[str, tuple[int, int]] = {}
    current: tuple[str, int] | None = None
    for index, line in enumerate(asm_text.splitlines()):
        begin = BEGIN_FN_RE.search(line)
        if begin:
            current = (begin.group(1), index)
            continue
        if current and END_FN_RE.match(line):
            spans[current[0]] = (current[1], index)
            current = None
    return spans


def _asm_blocks(lines: list[str], start: int, end: int) -> dict[str, tuple[int, int]]:
    blocks: dict[str, tuple[int, int]] = {}
    current: tuple[str, int] | None = None
    for index in range(start, end + 1):
        match = ASM_BLOCK_RE.match(lines[index])
        if not match:
            continue
        number = match.group(1) or match.group(2)
        if current and current[0] == number:
            continue
        if current:
            blocks[current[0]] = (current[1], index - 1)
        current = (number, index)
    if current:
        blocks[current[0]] = (current[1], end)
    return blocks


def _mir_kind(line: str) -> tuple[str, str] | None:
    if not line.startswith(" ") or MIR_SKIP_RE.match(line):
        return None
    body = line.split(";")[0]
    opcode = next((tok for tok in body.split() if MIR_OPCODE_RE.match(tok)), None)
    if opcode is None:
        return None
    return ("kill", opcode) if opcode == "KILL" else ("insn", opcode)


def _asm_kind(line: str) -> tuple[str, str] | None:
    if ASM_KILL_RE.match(line):
        return ("kill", "")
    match = ASM_INSN_RE.match(line)
    return ("insn", match.group(1)) if match else None


def _compatible(opcode: str, mnemonic: str) -> bool:
    stem = re.match(r"[a-z]+", opcode.lower())
    stem_text = STEM_ALIASES.get(stem.group(0), stem.group(0)) if stem else ""
    size = min(3, len(stem_text), len(mnemonic))
    return bool(size) and mnemonic[:size] == stem_text[:size]


def _align(mir: list[tuple[int, str]], asm: list[tuple[int, str]]) -> list[tuple[int, int]]:
    if len(mir) == len(asm):
        return [(m, a) for (m, _), (a, _) in zip(mir, asm)]
    rows, cols = len(mir), len(asm)
    score = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            best = max(score[i + 1][j], score[i][j + 1])
            if _compatible(mir[i][1], asm[j][1]):
                best = max(best, score[i + 1][j + 1] + 1)
            score[i][j] = best
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < rows and j < cols:
        if _compatible(mir[i][1], asm[j][1]) and score[i][j] == score[i + 1][j + 1] + 1:
            pairs.append((mir[i][0], asm[j][0]))
            i += 1
            j += 1
        elif score[i + 1][j] >= score[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def correlate_asm(mir_text: str, asm_text: str, function: str) -> dict[str, object] | None:
    fn_span = asm_function_spans(asm_text).get(function)
    if fn_span is None:
        return None
    asm_lines = asm_text.splitlines()
    mir_lines = mir_text.splitlines()
    asm_blocks = _asm_blocks(asm_lines, *fn_span)

    blocks: list[dict[str, object]] = []
    pairs: list[tuple[int, int]] = []
    for span, _ in mir_block_spans(mir_text):
        number = MIR_BLOCK_NUM_RE.match(span.name)
        asm_span = asm_blocks.get(number.group(1)) if number else None
        blocks.append({
            "name": span.name,
            "mirStart": span.start, "mirEnd": span.end,
            "asmStart": asm_span[0] if asm_span else None,
            "asmEnd": asm_span[1] if asm_span else None,
        })
        if asm_span is None:
            continue
        for kind in ("insn", "kill"):
            mir = [(i, k[1]) for i in range(span.start, span.end + 1)
                   if (k := _mir_kind(mir_lines[i])) and k[0] == kind]
            asm = [(i, k[1]) for i in range(asm_span[0], asm_span[1] + 1)
                   if (k := _asm_kind(asm_lines[i])) and k[0] == kind]
            pairs.extend(_align(mir, asm))

    if not any(b["asmStart"] is not None for b in blocks):
        return None
    return {
        "asmStart": fn_span[0],
        "asmEnd": fn_span[1],
        "blocks": blocks,
        "pairs": sorted(pairs),
    }
