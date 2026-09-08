"""Report emission: manifest.json + per-pass JSON chunks + frontend copy."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diff import FnChange

# Files copied into the report directory (paths relative to frontend/).
FRONTEND_FILES = (
    "index.html",
    "app.js",
    "style.css",
    "vendor/cytoscape.min.js",
    "vendor/dagre.min.js",
    "vendor/cytoscape-dagre.js",
)


# Asset references in index.html that get a cache-busting content-digest stamp.
ASSET_REF_RE = re.compile(
    r'(?P<attr>href|src)="(?P<path>[^"?#]+\.(?:js|css))(?:\?v=[0-9a-f]+)?"'
)


def _stamp_assets(report_dir: Path) -> None:
    """Rewrite index.html's asset URLs to <name>?v=<content digest>."""
    index = report_dir / "index.html"
    if not index.is_file():
        return

    def stamp(match: re.Match[str]) -> str:
        asset = report_dir / match.group("path")
        if not asset.is_file():
            return match.group(0)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
        return f'{match.group("attr")}="{match.group("path")}?v={digest}"'

    index.write_text(ASSET_REF_RE.sub(stamp, index.read_text()))


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
    # Synthetic pre-pipeline card; nothing to diff against, no CFG to show.
    is_input: bool = False
    # fn -> source map of the "after" snapshot (per line, [file index, line] or None).
    src_maps: dict[str, list[Any]] = field(default_factory=dict)
    # Pass-manager scope (ir lane only): "module" | "cgscc" | "function" | "loop".
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
        # Omit all-None maps to avoid a full-length null array per pass.
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


def _line_delta(pass_: ReportPass) -> dict[str, int] | None:
    """Lines added/removed across every function this pass touched."""
    if pass_.is_input:
        return None
    added = removed = 0
    for change in pass_.functions.values():
        fn_added, fn_removed = change.line_delta
        added += fn_added
        removed += fn_removed
    return {"added": added, "removed": removed}


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
            "lineDelta": _line_delta(p),
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
    _stamp_assets(report_dir)
    return data_dir / "manifest.json"
