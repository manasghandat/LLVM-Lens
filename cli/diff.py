"""Per-function snapshot pairing and dedupe.

A pass only *changed* a function when its snapshot differs from the previous
snapshot of that same function. Snapshot streams (opt -print-changed=quiet,
llc machine dumps) are per (pass, function); this module turns them into
before/after change records so the report can mark "changed only".

Items are duck-typed: anything with ``.function`` and ``.text`` attributes
works -- IrSnapshot (opt lane) and MachineFunction (backend lane).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Iterable, Protocol


class Snapshot(Protocol):
    function: str
    text: str


@dataclass(frozen=True)
class FnChange:
    function: str
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after

    @property
    def line_delta(self) -> tuple[int, int]:
        """(added, removed) line counts, the same tally the diff view shows.

        A replaced region counts on both sides, exactly as it renders: three
        lines rewritten into two is +2 -3, not +0 -1. Whole-text insertions
        (a function's first snapshot, machine IR before ISel has run) are all
        additions, which is what actually happened.
        """
        added = removed = 0
        matcher = difflib.SequenceMatcher(
            None, self.before.splitlines(), self.after.splitlines(), autojunk=False,
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
        return added, removed

    def unified_diff(self, context: int = 2) -> str:
        return "".join(difflib.unified_diff(
            self.before.splitlines(), self.after.splitlines(),
            fromfile=f"before/{self.function}", tofile=f"after/{self.function}",
            n=context, lineterm="\n",
        ))


def pair_snapshots(items: Iterable[Snapshot]) -> dict[str, list[FnChange]]:
    """Pair each snapshot with its predecessor per function, in stream order.

    Returns ``{function: [FnChange, ...]}``. The first snapshot of a function
    pairs with an empty "before"; an unchanged pair (before == after) means
    the pass did not modify that function.
    """
    by_function: dict[str, list[FnChange]] = {}
    previous: dict[str, str] = {}
    for item in items:
        before = previous.get(item.function, "")
        change = FnChange(item.function, before, item.text)
        previous[item.function] = change.after
        by_function.setdefault(item.function, []).append(change)
    return by_function
