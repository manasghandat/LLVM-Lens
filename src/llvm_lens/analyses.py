"""Program-structure graphs derived from captured IR (PDT, CDG, DDG, PDG, MDG, CG, LNT).

Everything is computed in Python from the IR text the pipeline already captured
(the same source the CFG is built from), so none of it depends on a particular
LLVM version. DDG is instruction-level (def-use over SSA values); PDG is
block-level (control + block-aggregated data edges); MDG is a conservative
last-writer-per-pointer approximation with no alias analysis.
"""

from __future__ import annotations

import re

from .cfg import Block, _render_dot, ir_cfg
from .parsers.print_changed import split_module_functions

# "  %1 = add i32 %0, 5" -> value "1", rhs "add i32 %0, 5".
DEF_RE = re.compile(r"^\s*%([\w.]+)\s*=\s*(.*)$")
# SSA value operands: "%0", "%foo.1" (types like "%struct.x" never define a value,
# so they simply produce no edge).
OPERAND_RE = re.compile(r"%([\w.]+)")
# A direct call: "call i32 @foo(...)" / "tail call ... @bar(...)" / "invoke ... @baz(".
CALL_RE = re.compile(r"\b(?:call|invoke)\b[^@]*@([\w.\-]+)")
# A memory instruction, optional "%v = " prefix.
MEM_OP_RE = re.compile(r"^\s*(?:%[\w.]+\s*=\s*)?(load|store|atomicrmw|cmpxchg)\b")
# The memory pointer: "ptr %p" or "ptr @g".
PTR_RE = re.compile(r"ptr\s+(%[\w.]+|@[\w.]+)")

EXIT = "__exit__"


def _dedupe(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(edges))


def _dominators(entry: str, names: list[str], succ: dict[str, list[str]]) -> dict[str, set[str]]:
    pred: dict[str, list[str]] = {n: [] for n in names}
    for n in names:
        for s in succ.get(n, ()):
            if s in pred:
                pred[s].append(n)
    dom = {n: set(names) for n in names}
    dom[entry] = {entry}
    changed = True
    while changed:
        changed = False
        for n in names:
            if n == entry:
                continue
            ps = pred[n]
            new = set.intersection(*(dom[p] for p in ps)) if ps else set()
            new.add(n)
            if new != dom[n]:
                dom[n] = new
                changed = True
    return dom


def _immediate(dom: dict[str, set[str]], node: str) -> str | None:
    strict = dom[node] - {node}
    for cand in strict:
        if all(o in dom[cand] for o in strict if o != cand):
            return cand
    return None


def _postdominators(names: list[str], succ: dict[str, list[str]]) -> dict[str, set[str]]:
    rev: dict[str, list[str]] = {n: [] for n in names}
    rev[EXIT] = []
    for n in names:
        for s in succ.get(n, ()):
            if s in rev:
                rev[s].append(n)
    for n in names:
        if not succ.get(n):
            rev[EXIT].append(n)
    return _dominators(EXIT, [*names, EXIT], rev)


def _control_dependence(
    names: list[str],
    succ: dict[str, list[str]],
    ipdom: dict[str, str | None],
) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for x in names:
        for s in succ.get(x, ()):
            if s not in names:
                continue
            n: str | None = s
            while n is not None and n != ipdom.get(x) and n in names:
                if n != x:
                    edges.append((x, n))
                n = ipdom.get(n)
    return _dedupe(edges)


def _block_data_dependence(blocks: list[Block]) -> list[tuple[str, str]]:
    def_block: dict[str, str] = {}
    uses: list[tuple[str, str]] = []
    for b in blocks:
        for line in b.code:
            m = DEF_RE.match(line)
            if m:
                def_block[m.group(1)] = b.name
            for op in OPERAND_RE.findall(line):
                uses.append((op, b.name))
    edges = []
    for value, block in uses:
        if value in def_block and def_block[value] != block:
            edges.append((def_block[value], block))
    return _dedupe(edges)


def _natural_loop(header: str, latch: str, pred: dict[str, list[str]]) -> set[str]:
    body = {latch}
    stack = [latch]
    while stack:
        x = stack.pop()
        for p in pred.get(x, ()):
            if p != header and p not in body:
                body.add(p)
                stack.append(p)
    body.add(header)
    return body


def _function_graphs(fn_ir: str) -> dict[str, str] | None:
    entry, blocks = ir_cfg(fn_ir)
    if not blocks:
        return None
    names = [b.name for b in blocks]
    name_set = set(names)
    succ = {b.name: [s for s in b.successors if s in name_set] for b in blocks}
    pred: dict[str, list[str]] = {n: [] for n in names}
    for n in names:
        for s in succ[n]:
            pred[s].append(n)

    dom = _dominators(entry, names, succ)
    pdom = _postdominators(names, succ)
    ipdom = {n: _immediate(pdom, n) for n in names}
    code_map = {b.name: list(b.code) for b in blocks}

    # Post-dominator tree (rooted at the virtual exit). Labels show the block's
    # instructions rather than its bare name, which is just a number for unnamed
    # blocks.
    pdt_nodes = [(n, code_map.get(n) or [n]) for n in names] + [(EXIT, [EXIT])]
    pdt_edges = [(ipdom[n], n) for n in names if ipdom[n] is not None]
    pdt = _render_dot(pdt_nodes, pdt_edges)

    # Control dependence graph.
    cdg_edges = _control_dependence(names, succ, ipdom)
    cfg_nodes = [(b.name, list(b.code)) for b in blocks]
    cdg = _render_dot(cfg_nodes, cdg_edges)

    # Data dependence (instruction-level def-use) and block-level PDG.
    ddg = _data_dependence(blocks)
    data_edges = _block_data_dependence(blocks)
    pdg = _render_dot(cfg_nodes, _dedupe(cdg_edges + data_edges))

    # Loop nest tree.
    lnt = _loop_nest_tree(entry, names, succ, dom, code_map)

    return {
        "pdt": pdt,
        "cdg": cdg,
        "ddg": ddg,
        "pdg": pdg,
        "mdg": _memory_dependence(blocks),
        "lnt": lnt,
    }


def _data_dependence(blocks: list[Block]) -> str:
    defs: dict[str, str] = {}
    instructions: list[tuple[str, str]] = []  # (value, rhs) in order
    for b in blocks:
        for line in b.code:
            m = DEF_RE.match(line)
            if m:
                defs[m.group(1)] = line
                instructions.append((m.group(1), m.group(2)))
    edges: list[tuple[str, str]] = []
    for value, rhs in instructions:
        for op in OPERAND_RE.findall(rhs):
            if op in defs:
                edges.append((f"%{op}", f"%{value}"))
    nodes = [(f"%{v}", [defs[v]]) for v, _ in instructions]
    return _render_dot(nodes, _dedupe(edges))


def _memory_dependence(blocks: list[Block]) -> str:
    nodes: list[tuple[str, list[str]]] = []
    edges: list[tuple[str, str]] = []
    last_writer: dict[str, str] = {}
    for b in blocks:
        for line in b.code:
            m = MEM_OP_RE.match(line)
            if not m:
                continue
            ptrs = PTR_RE.findall(line)
            if not ptrs:
                continue
            cls = ptrs[-1] if ptrs[-1].startswith("@") else "*"
            name = f"m{len(nodes)}"
            nodes.append((name, [line]))
            if cls in last_writer:
                edges.append((last_writer[cls], name))
            if m.group(1) != "load":
                last_writer[cls] = name
    return _render_dot(nodes, edges)


def _loop_nest_tree(
    entry: str,
    names: list[str],
    succ: dict[str, list[str]],
    dom: dict[str, set[str]],
    code_map: dict[str, list[str]],
) -> str:
    pred: dict[str, list[str]] = {n: [] for n in names}
    for n in names:
        for s in succ.get(n, ()):
            if s in pred:
                pred[s].append(n)

    loops: dict[str, set[str]] = {}
    for n in names:
        for s in succ.get(n, ()):
            if s in dom.get(n, ()) and s not in loops:
                loops[s] = _natural_loop(s, n, pred)

    edges: list[tuple[str, str]] = []
    for h in loops:
        parent = None
        for h2, body2 in loops.items():
            if h2 != h and h in body2:
                if parent is None or len(body2) < len(loops[parent]):
                    parent = h2
        if parent is not None:
            edges.append((parent, h))
        elif h != entry:
            edges.append((entry, h))

    seen: set[str] = set()
    nodes: list[tuple[str, list[str]]] = []
    for x in [entry, *loops]:
        if x not in seen:
            seen.add(x)
            nodes.append((x, code_map.get(x) or [x]))
    return _render_dot(nodes, edges)


def _call_graph(module_text: str) -> str:
    funcs = split_module_functions(module_text)
    nodes: list[tuple[str, list[str]]] = [(fn, [fn]) for fn in funcs]
    edges: list[tuple[str, str]] = []
    seen = set(funcs)
    for fn, text in funcs.items():
        for line in text.splitlines():
            for callee in CALL_RE.findall(line):
                if callee != fn:
                    edges.append((fn, callee))
                    if callee not in seen:
                        seen.add(callee)
                        nodes.append((callee, [callee]))
    return _render_dot(nodes, _dedupe(edges))


def compute_analyses(module_text: str) -> dict:
    """Compute every analysis for the module; returns the report's `analyses` payload."""
    functions: dict[str, dict[str, str]] = {}
    for fn, text in split_module_functions(module_text).items():
        graphs = _function_graphs(text)
        if graphs:
            functions[fn] = graphs
    return {"callGraph": _call_graph(module_text), "functions": functions}
