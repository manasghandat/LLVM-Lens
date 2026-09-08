"""Parse opt's -debug-pass-manager output (new pass manager)."""

from __future__ import annotations

import re
from dataclasses import dataclass

RUN_PASS_RE = re.compile(r"^Running pass: (\S+) on (.+)$")
RUN_ANALYSIS_RE = re.compile(r"^Running analysis: (\S+) on (.+?)( \(cached\))?$")
INVALIDATE_RE = re.compile(r"^Invalidating analysis: (\S+) on (.+)$")

# Classify a function field into its pass-manager scope (prefix matching).
_MODULE_RE = re.compile(r"^\[module\]")
_CGSCC_RE = re.compile(r"^\(")  # "(name) (N node[s])"
# Loop scope: "loop %id in function name" (LLVM 22) or "<unnamed loop>" (18).
_LOOP_RE = re.compile(r"^(loop\s+|<)")


def scope_of(function: str) -> str:
    """Return the pass-manager scope for a PassRun.function value."""
    f = function.strip()
    if _MODULE_RE.match(f):
        return "module"
    if _CGSCC_RE.match(f):
        return "cgscc"
    if _LOOP_RE.match(f):
        return "loop"
    return "function"


@dataclass(frozen=True)
class AnalysisEvent:
    name: str
    function: str
    cached: bool = False


@dataclass(frozen=True)
class PassRun:
    name: str
    function: str  # "[module]" for module-level passes
    index: int  # 1-based, in order of appearance
    line: int  # 1-based line of the "Running pass" line in the stream
    analyses: tuple[AnalysisEvent, ...] = ()
    invalidated: tuple[AnalysisEvent, ...] = ()


def parse_pass_runs(stderr: str) -> list[PassRun]:
    runs: list[PassRun] = []
    current: PassRun | None = None
    analyses: list[AnalysisEvent] = []
    invalidated: list[AnalysisEvent] = []

    def flush() -> None:
        nonlocal current, analyses, invalidated
        if current is not None:
            runs.append(PassRun(
                current.name, current.function, current.index, current.line,
                tuple(analyses), tuple(invalidated),
            ))
        analyses, invalidated = [], []

    for line_no, line in enumerate(stderr.splitlines(), start=1):
        match = RUN_PASS_RE.match(line)
        if match:
            flush()
            current = PassRun(match.group(1), match.group(2), len(runs) + 1, line_no)
            continue
        match = RUN_ANALYSIS_RE.match(line)
        if match and current is not None:
            analyses.append(AnalysisEvent(
                match.group(1), match.group(2), cached=bool(match.group(3)),
            ))
            continue
        match = INVALIDATE_RE.match(line)
        if match and current is not None:
            invalidated.append(AnalysisEvent(match.group(1), match.group(2)))
    flush()
    return runs
