"""Per-line blame: which pass put each line of a snapshot where it is.

`opt` only dumps when a pass changed the IR, so every line has one pass that
put it there. This module walks the whole dump stream — every repeat of a pass
included — and records, per function, that lineage: the input state, then every
state after it, each line tagged with the writers that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .emit import MODULE_FN, ReportPass
from .parsers.debug_pass_manager import PassRun
from .parsers.print_changed import IrSnapshot, split_module_functions

INPUT_NAME = "Input IR"
KINDS = ("created", "rewritten", "renamed")

SCC_RE = re.compile(r"^\(([^(),]+)\)$")
LOOP_RE = re.compile(r"^loop .* in function (.+)$")
SSA_RE = re.compile(r"%[\w.]+")
LABEL_RE = re.compile(r"\b\d+:")

# opt driver passes clang never runs; no card, and no state worth blaming.
DRIVER_PASSES = frozenset({"VerifierPass", "PrintModulePass"})


# --- naming -------------------------------------------------------------------


def canonical_fn(name: str) -> str:
    """The entity a dump belongs to, as the report tracks it."""
    match = SCC_RE.match(name) or LOOP_RE.match(name)
    return match.group(1) if match else name


def entity_text(entity: str, body: str) -> str:
    """The part of one module-scope dump body that belongs to *entity*."""
    if entity == MODULE_FN:
        return body
    bodies = split_module_functions(body)
    if not bodies:
        return body
    if entity.startswith("(") and entity.endswith(")"):
        members = [name.strip() for name in entity[1:-1].split(",")]
        chosen = [bodies[name] for name in members if name in bodies]
        return "\n\n".join(chosen) if chosen else body
    return bodies.get(entity, body)


def dump_states(dump: IrSnapshot) -> dict[str, str]:
    """Every entity this dump is authoritative about, as it stood just then."""
    entity = canonical_fn(dump.function)
    out: dict[str, str] = {}
    if dump.module_scope or entity == MODULE_FN:
        # A module-scope body is the whole module, whoever the header names, so
        # it fixes the state of every function in it, not just its own entity.
        out[MODULE_FN] = dump.ir
        out.update(split_module_functions(dump.ir))
    if entity != MODULE_FN:
        out[entity] = entity_text(entity, dump.ir)
    return out


def split_lines(text: str) -> list[str]:
    """The lines of *text*, split the way the diff view splits it."""
    if not text:
        return []
    body = text[:-1] if text.endswith("\n") else text
    return body.split("\n")


# --- the walk -----------------------------------------------------------------


def blame_key(line: str) -> str:
    """SSA renumbering (%3 -> %7) and a relabelled block are still the same line."""
    return LABEL_RE.sub(":", SSA_RE.sub("%", line)).strip()


@dataclass(frozen=True)
class Entry:
    """One pass writing one line."""

    run: int
    name: str
    kind: str


@dataclass
class State:
    """A function as of one run, with every line's writers, oldest first."""

    run: int
    lines: list[str]
    history: list[list[Entry]]


def _plan(before: Sequence[str], after: Sequence[str]) -> list[tuple[int | None, str | None]]:
    """For each line of *after*: the line of *before* it came from, and how.

    A None source means the line is new here; a None kind means it is untouched.
    Common head and tail are stripped first, as the diff view does, so a run of
    changes in the middle is all the matcher ever sees.
    """
    head = 0
    while head < len(before) and head < len(after) and before[head] == after[head]:
        head += 1
    tail = 0
    while (tail < len(before) - head and tail < len(after) - head
           and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]):
        tail += 1
    old = list(before[head:len(before) - tail])
    new = list(after[head:len(after) - tail])

    plan: list[tuple[int | None, str | None]] = [(None, None)] * len(after)
    for k in range(head):
        plan[k] = (k, None)
    for k in range(tail):
        plan[len(after) - 1 - k] = (len(before) - 1 - k, None)

    # The same longest-common-subsequence walk the diff view runs, so that a
    # line this calls rewritten is a line the diff shows as a change.
    n, m = len(old), len(new)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, below = dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = below[j + 1] + 1 if old[i] == new[j] else max(below[j], row[j + 1])

    dels: list[int] = []
    adds: list[int] = []

    def flush() -> None:
        # Every change run pairs its deletions with its insertions in order.
        for k, j in enumerate(adds):
            source = dels[k] if k < len(dels) else None
            kind = "created" if source is None else (
                "renamed" if blame_key(old[source]) == blame_key(new[j]) else "rewritten"
            )
            plan[head + j] = (head + source if source is not None else None, kind)
        dels.clear()
        adds.clear()

    i = j = 0
    while i < n or j < m:
        if i < n and j < m and old[i] == new[j]:
            flush()
            plan[head + j] = (head + i, None)
            i += 1
            j += 1
        elif j >= m or (i < n and dp[i + 1][j] >= dp[i][j + 1]):
            dels.append(i)
            i += 1
        else:
            adds.append(j)
            j += 1
    flush()
    return plan


# --- source-line diff: how the IR lines for each source line changed ------------


def source_line_diff(
    before_text: str, before_map: Sequence[list[int] | None] | None,
    after_text: str, after_map: Sequence[list[int] | None] | None,
) -> dict[tuple[int, int], dict[str, int]]:
    """Per-source-line change counts across one pass, matched the way the diff view does.

    The IR lines *before* the pass and *after* are each grouped by the source line
    they map to (via the encoded src_maps). For each source line present after the
    pass, the two groups are matched with `_plan` — the same LCS walk the diff view
    runs — so a line counted rewritten is a line the diff would show as a change.
    Returns {(file, line): {"created", "rewritten", "renamed", "removed", "kept"}}.
    """
    before_lines = split_lines(before_text)
    after_lines = split_lines(after_text)

    def group_by_src(text_lines: list[str], src_map: Sequence[list[int] | None] | None,
                     ) -> dict[tuple[int, int], list[str]]:
        groups: dict[tuple[int, int], list[str]] = {}
        for index, ref in enumerate(src_map or []):
            if ref is None or index >= len(text_lines):
                continue
            groups.setdefault((ref[0], ref[1]), []).append(text_lines[index])
        return groups

    before_groups = group_by_src(before_lines, before_map)
    after_groups = group_by_src(after_lines, after_map)

    result: dict[tuple[int, int], dict[str, int]] = {}
    for src, after_group in after_groups.items():
        before_group = before_groups.get(src, [])
        plan = _plan(before_group, after_group)
        counts = {"created": 0, "rewritten": 0, "renamed": 0, "kept": 0}
        matched_before = 0
        for source, kind in plan:
            if kind is None:
                if source is not None:
                    counts["kept"] += 1
                    matched_before += 1
                continue
            counts[kind] += 1
            if source is not None:
                matched_before += 1
        counts["removed"] = len(before_group) - matched_before
        result[src] = counts
    for src, before_group in before_groups.items():
        if src not in result:
            result[src] = {"created": 0, "rewritten": 0, "renamed": 0,
                           "kept": 0, "removed": len(before_group)}
    return result


def advance(
    lines: Sequence[str], history: Sequence[Sequence[Entry]], run: int, name: str, text: str,
) -> tuple[list[str], list[list[Entry]]]:
    """The lines of *text*, each carrying its writers, plus this pass's own."""
    after = split_lines(text)
    out_lines: list[str] = []
    out_history: list[list[Entry]] = []
    for index, (source, kind) in enumerate(_plan(list(lines), after)):
        out_lines.append(after[index])
        prior = list(history[source]) if source is not None and source < len(history) else []
        if kind is None:
            out_history.append(prior)
        else:
            out_history.append(prior + [Entry(run, name, kind)])
    return out_lines, out_history


# --- source-line history: per source line, every pass that changed it -----------


def source_line_history(
    transitions: Sequence[tuple[str, int, str, Sequence[list[int] | None] | None,
                              str, Sequence[list[int] | None] | None]],
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Aggregate per-source-line diffs across the whole pipeline into a timeline.

    *transitions* is one entry per pass: (pass_name, run_index, before_text,
    before_map, after_text, after_map). For each source line, collect every pass
    that changed it, oldest first, with the per-pass counts source_line_diff
    computed. A source line that a pass left untouched is omitted from that pass —
    the history only names the passes that touched it.
    """
    history: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for name, run, before_text, before_map, after_text, after_map in transitions:
        if before_text == after_text:
            continue
        diffs = source_line_diff(before_text, before_map, after_text, after_map)
        for src, counts in diffs.items():
            if not counts["created"] and not counts["rewritten"] and not counts["renamed"] and not counts["removed"]:
                continue
            history.setdefault(src, []).append(
                {"pass": name, "run": run, **counts})
    return history


def walk(states: Sequence[tuple[int, str, str]]) -> list[State]:
    """states: (run, pass, text) wherever the text changed, oldest first.

    Run 0 is the state a lane was handed, so its lines start unowned; a lane
    that starts at a pass (the machine lane) has every line created by it.
    """
    out: list[State] = []
    lines: list[str] = []
    history: list[list[Entry]] = []
    for index, (run, name, text) in enumerate(states):
        if index == 0 and run == 0:
            lines = split_lines(text)
            history = [[] for _ in lines]
        else:
            lines, history = advance(lines, history, run, name, text)
        out.append(State(run, lines, history))
    return out


# --- lane A: the dump stream, run by run --------------------------------------


@dataclass(frozen=True)
class RunState:
    """One run of a pass, and the dumps it left behind."""

    run: PassRun
    dumps: tuple[IrSnapshot, ...] = ()


@dataclass(frozen=True)
class Slot:
    """One card: a pass, and every run of it that left a state."""

    run_index: int
    name: str
    run: PassRun
    runs: tuple[RunState, ...] = ()

    @property
    def dumps(self) -> tuple[IrSnapshot, ...]:
        """The state the card leaves behind: its last run's dump."""
        return self.runs[-1].dumps if self.runs else ()


def dumps_by_run(runs: Sequence[PassRun], dumps: Sequence[IrSnapshot]) -> dict[int, list[IrSnapshot]]:
    """Match each dump to the run it came from: the last run before its header."""
    by_name: dict[str, list[PassRun]] = {}
    for run in runs:
        by_name.setdefault(run.name, []).append(run)
    found: dict[int, list[IrSnapshot]] = {}
    for dump in dumps:
        chosen = None
        for run in by_name.get(dump.pass_name, ()):
            if run.line < dump.line:
                chosen = run
            else:
                break
        if chosen is not None:
            found.setdefault(chosen.index, []).append(dump)
    return found


def lane_a_slots(runs: Sequence[PassRun], dumps: Sequence[IrSnapshot]) -> list[Slot]:
    """The pipeline as cards: one per pass, holding every run that left a state."""
    by_run = dumps_by_run(runs, dumps)
    dumping = {dump.pass_name for dump in dumps}
    order: list[str] = []
    first: dict[str, PassRun] = {}
    collected: dict[str, list[RunState]] = {}
    for run in runs:
        if run.name in DRIVER_PASSES:
            continue
        left = tuple(by_run.get(run.index, ()))
        # A run that left nothing is only worth a card if its pass never dumps.
        if not left and (run.name in dumping or run.name in collected):
            continue
        if run.name not in collected:
            order.append(run.name)
            first[run.name] = run
            collected[run.name] = []
        collected[run.name].append(RunState(run, left))
    # run_index is the pass's first *run*, so a card can still name a run.
    return [
        Slot(first[name].index, name, first[name], tuple(collected[name]))
        for name in order
    ]


# --- the timelines ------------------------------------------------------------


@dataclass
class Timeline:
    """What each entity looked like, over one lane, and when it changed."""

    states: dict[str, list[tuple[int, str, str]]] = field(default_factory=dict)
    changes: dict[int, list[str]] = field(default_factory=dict)

    def record(self, name: str, run: int, pass_name: str, text: str) -> bool:
        """Note an entity's text as of *run*, if it is not what it already was."""
        line = self.states.setdefault(name, [])
        if line and line[-1][2] == text:
            return False
        line.append((run, pass_name, text))
        self.changes.setdefault(run, []).append(name)
        return True

    def seed(self, text: str) -> None:
        """The input state: the module a lane was handed, and its functions."""
        for name, body in {MODULE_FN: text, **split_module_functions(text)}.items():
            self.states[name] = [(0, INPUT_NAME, body)]

    def names(self) -> Iterable[str]:
        return self.states

    def at(self, name: str, run: int) -> str:
        """The text of *name* as of *run*; "" if it did not exist yet."""
        text = ""
        for state_run, _pass, state_text in self.states.get(name, ()):
            if state_run > run:
                break
            text = state_text
        return text

    def changed_at(self, run: int) -> list[str]:
        return self.changes.get(run, [])


def lane_a_timeline(slots: Sequence[Slot], input_ir: str | None) -> Timeline:
    """Follow every entity through the whole dump stream, repeats included."""
    timeline = Timeline()
    if input_ir is not None:
        timeline.seed(input_ir)
    # Cards group a pass's runs, so flatten and re-sort by run ordinal.
    states = [state for slot in slots for state in slot.runs if state.dumps]
    for state in sorted(states, key=lambda s: s.run.index):
        for name, text in dump_states(state.dumps[-1]).items():
            timeline.record(name, state.run.index, state.run.name, text)
    return timeline


def mir_timeline(passes: Sequence[ReportPass]) -> Timeline:
    """Follow every machine function through the cards, as their diffs show it.

    A card is one pass id, however many times it ran, so its before/after is the
    whole span it covers — and that span is the state blame may name.
    """
    timeline = Timeline()
    for card in passes:
        for name, change in card.functions.items():
            timeline.record(name, card.run_index, card.name, change.after)
    return timeline


# --- the artifact the report ships --------------------------------------------


def blame_document(lane: str, timeline: Timeline) -> dict[str, Any]:
    """One lane's walks, as small as they go: pass names and line chains interned."""
    names = [INPUT_NAME]
    name_ids = {INPUT_NAME: 0}
    events: list[list[int]] = []
    event_ids: dict[tuple[int, int, str], int] = {}
    chains: list[list[int]] = [[]]
    chain_ids: dict[tuple[int, ...], int] = {(): 0}

    def name_id(name: str) -> int:
        if name not in name_ids:
            name_ids[name] = len(names)
            names.append(name)
        return name_ids[name]

    def event_id(entry: Entry) -> int:
        key = (entry.run, name_id(entry.name), entry.kind)
        if key not in event_ids:
            event_ids[key] = len(events)
            events.append([entry.run, key[1], KINDS.index(entry.kind)])
        return event_ids[key]

    def chain_id(history: Sequence[Entry]) -> int:
        key = tuple(event_id(entry) for entry in history)
        if key not in chain_ids:
            chain_ids[key] = len(chains)
            chains.append(list(key))
        return chain_ids[key]

    functions: dict[str, Any] = {}
    for name, states in timeline.states.items():
        walked = walk(states)
        functions[name] = {
            "text": walked[-1].lines if walked else [],
            "states": [
                {"run": state.run, "h": [chain_id(h) for h in state.history]}
                for state in walked
            ],
        }
    return {
        "lane": lane,
        "names": names,
        "kinds": list(KINDS),
        "events": events,
        "hist": chains,
        "functions": functions,
    }
