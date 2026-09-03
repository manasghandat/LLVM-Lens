"""Report emission: manifest.json + per-pass JSON chunks + frontend copy.

Layout:

    report/
      index.html, app.js, style.css     (copied from frontend/)
      data/
        manifest.json                   metadata + ordered pass list
        manifest.js                     same data as a script (file://-safe)
        pass-<id>.json                  per-pass detail chunk
        pass-<id>.js                    same data as a script (file://-safe)

The .js wrappers exist because browsers refuse ``fetch()`` on ``file://``
URLs; the frontend tries fetch first and falls back to script injection.
``manifest.js`` also initializes the ``window.__LLVM_LENS_DATA__`` store the
chunk scripts fill.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diff import FnChange

# Files copied into the report directory (paths relative to frontend/).
# vendor/ holds the UMD builds of the CFG graph stack (cytoscape + dagre),
# so reports stay fully self-contained and file://-safe.
FRONTEND_FILES = (
    "index.html",
    "app.js",
    "style.css",
    "vendor/cytoscape.min.js",
    "vendor/dagre.min.js",
    "vendor/cytoscape-dagre.js",
)


@dataclass
class ReportPass:
    id: int
    lane: str  # "ir" | "mir"
    name: str  # display name
    pass_id: str | None  # canonical pass id (the "(...)" in headers), if any
    run_index: int  # 1-based position in its lane
    time_ms: float | None
    changed: bool
    functions: dict[str, FnChange] = field(default_factory=dict)
    dots: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    analyses: dict[str, list[str]] = field(default_factory=dict)  # run/cached/invalidated
    log: str = ""
    spills: dict[str, int] = field(default_factory=dict)  # mir only: fn -> count
    reg_map: dict[str, dict[str, str]] = field(default_factory=dict)  # mir only
    asm: str | None = None  # final assembly text, attached to the last mir pass
    is_custom: bool = False  # named via --custom-pass (plugin-loaded pass)
    # The synthetic pre-pipeline card (cli/main.py build_input_pass). Nothing
    # precedes it, so there is nothing to diff against, and its "function" is
    # a whole module, so a control-flow graph of it is meaningless -- the
    # viewer offers only the IR and Source views for it.
    is_input: bool = False
    # fn -> source map of the *after* snapshot: per line, [file index, source
    # line] or None. Built by cli/sourcemap.py; empty without debug info. The
    # before side needs no map of its own -- it is the previous pass's after.
    src_maps: dict[str, list[Any]] = field(default_factory=dict)
    # Pass-manager scope (ir lane only): "module" | "cgscc" | "function" |
    # "loop", inferred from the -debug-pass-manager "on <target>" field. Null for
    # the mir lane, which gets its hierarchy from -debug-pass=Structure instead.
    scope: str | None = None


def _pass_json(pass_: ReportPass) -> dict[str, Any]:
    functions: dict[str, Any] = {}
    for fn, change in pass_.functions.items():
        functions[fn] = {
            "before": change.before,
            "after": change.after,
            "changed": change.changed,
        }
        dot_before, dot_after = pass_.dots.get(fn, (None, None))
        if dot_before:
            functions[fn]["dotBefore"] = dot_before
        if dot_after:
            functions[fn]["dotAfter"] = dot_after
        # Omit all-None maps: a snapshot with no resolvable location would
        # otherwise cost a full-length array of nulls per pass.
        src_after = pass_.src_maps.get(fn)
        if src_after and any(src_after):
            functions[fn]["srcAfter"] = src_after

    entry: dict[str, Any] = {
        "id": pass_.id,
        "lane": pass_.lane,
        "name": pass_.name,
        "passId": pass_.pass_id,
        "runIndex": pass_.run_index,
        "timeMs": pass_.time_ms,
        "changed": pass_.changed,
        "isCustom": pass_.is_custom,
        "functions": functions,
        "analyses": pass_.analyses,
        "log": pass_.log,
    }
    if pass_.lane == "mir":
        entry["spills"] = pass_.spills
        entry["regMap"] = pass_.reg_map
        entry["asm"] = pass_.asm
    return entry


def _manifest_json(passes: list[ReportPass], metadata: dict[str, Any]) -> dict[str, Any]:
    pass_list = [
        {
            "id": p.id,
            "lane": p.lane,
            "name": p.name,
            "passId": p.pass_id,
            "runIndex": p.run_index,
            "timeMs": p.time_ms,
            "changed": p.changed,
            "isCustom": p.is_custom,
            "isInput": p.is_input,
            "spillCount": sum(p.spills.values()) if p.spills else None,
            "analysisCounts": {
                "run": len(p.analyses.get("run", [])),
                "cached": len(p.analyses.get("cached", [])),
                "invalidated": len(p.analyses.get("invalidated", [])),
            },
            "functions": sorted(p.functions),
            "scope": p.scope,
        }
        for p in passes
    ]
    return {
        "schemaVersion": 1,
        "metadata": metadata,
        "passes": pass_list,
    }


def _write_json_plus_script(path: Path, data: dict[str, Any], assign: str) -> None:
    text = json.dumps(data, indent=1)
    path.with_suffix(".json").write_text(text + "\n")
    path.with_suffix(".js").write_text(f"{assign} = {text};\n")


def emit_report(
    report_dir: str | Path,
    *,
    passes: list[ReportPass],
    metadata: dict[str, Any],
    frontend_dir: str | Path,
) -> Path:
    """Write the report into *report_dir*; returns the manifest path."""
    report_dir = Path(report_dir)
    data_dir = report_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    manifest = _manifest_json(passes, metadata)
    _write_json_plus_script(
        data_dir / "manifest",
        manifest,
        "window.__LLVM_LENS_MANIFEST__;\n"
        "window.__LLVM_LENS_DATA__ = {}\n"
        "window.__LLVM_LENS_MANIFEST__",
    )
    for pass_ in passes:
        _write_json_plus_script(
            data_dir / f"pass-{pass_.id}",
            _pass_json(pass_),
            f'window.__LLVM_LENS_DATA__["pass-{pass_.id}"]',
        )

    frontend_dir = Path(frontend_dir)
    for name in FRONTEND_FILES:
        source = frontend_dir / name
        if source.is_file():
            destination = report_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return data_dir / "manifest.json"
