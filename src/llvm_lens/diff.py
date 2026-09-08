"""Per-function snapshot pairing and dedupe."""

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
        """Line counts (added, removed), same tally the diff view shows."""
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
    """Pair each snapshot with its predecessor per function, in stream order."""
    by_function: dict[str, list[FnChange]] = {}
    previous: dict[str, str] = {}
    for item in items:
        before = previous.get(item.function, "")
        change = FnChange(item.function, before, item.text)
        previous[item.function] = change.after
        by_function.setdefault(item.function, []).append(change)
    return by_function
