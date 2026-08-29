"""Parse ``-time-passes`` output.

Each timed block looks like:

      Total Execution Time: 0.0007 seconds (0.0011 wall clock)

       ---User Time---   --System Time--   --User+System--   ---Wall Time---  --- Name ---
       0.0000 (  0.0%)   ...                 ...              TargetIRAnalysis
       ...

The final "Pass execution timing report" summary block (preceded by a
``===...===`` banner, the centered title line, then another ``===...===``)
tables every pass of the whole run; that is the primary per-pass timing
source (matched by name in main.py). Earlier interleaved blocks belong to
the pass whose section precedes them (attributed by line).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TOTAL_TIME_RE = re.compile(
    r"^\s*Total Execution Time: ([\d.]+) seconds \(([\d.]+) wall clock\)$"
)
# Four time columns of "0.0000 ( 12.9%)", then the (possibly spaced) name.
# Captures the User+System column (same semantics as block total_seconds).
ROW_RE = re.compile(
    r"^\s*[\d.]+\s+\([\s\d.]+%\)\s+"      # ---User Time---
    r"[\d.]+\s+\([\s\d.]+%\)\s+"          # --System Time--
    r"([\d.]+)\s+\([\s\d.]+%\)\s+"        # --User+System--  <- captured
    r"[\d.]+\s+\([\s\d.]+%\)\s+"          # ---Wall Time---
    r"(.+)$"                              # --- Name ---
)
# The final summary banner: "===---...===" lines plus a centered
# "Pass execution timing report" line (no === on it in LLVM 22).
BANNER_RE = re.compile(r"Pass execution timing report")


@dataclass(frozen=True)
class TimeBlock:
    line: int  # 1-based line of "Total Execution Time" in the stream
    total_seconds: float
    wall_seconds: float
    rows: tuple[tuple[str, float], ...] = ()  # (name, user+system seconds)
    is_summary: bool = False


def parse_time_passes(stderr: str) -> list[TimeBlock]:
    lines = stderr.splitlines()
    blocks: list[TimeBlock] = []
    summary_seen = False

    for index, line in enumerate(lines, start=1):
        if BANNER_RE.search(line):  # banner is centered with leading spaces
            summary_seen = True
            continue
        match = TOTAL_TIME_RE.match(line)
        if not match:
            continue
        rows: list[tuple[str, float]] = []
        for row in lines[index:]:
            row_match = ROW_RE.match(row)
            if row_match:
                rows.append((row_match.group(2).strip(), float(row_match.group(1))))
            elif not rows:
                continue  # blank or "---User Time---" header before the table
            else:
                break  # blank/non-row after the table
        blocks.append(TimeBlock(
            line=index,
            total_seconds=float(match.group(1)),
            wall_seconds=float(match.group(2)),
            rows=tuple(rows),
            is_summary=summary_seen,
        ))
        summary_seen = False
    return blocks
