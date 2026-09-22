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

MODULE_FN = "[module]"

# Files copied into the report directory (paths relative to frontend/).
FRONTEND_FILES = (
    "index.html",
    "app.js",
    "ai.js",
    "style.css",
    "vendor/cytoscape.min.js",
    "vendor/dagre.min.js",
    "vendor/cytoscape-dagre.js",
)

AI_CONFIG_ASSIGN = "window.__LLVM_LENS_AI_CONFIG__"


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
class RunSegment:
    """One run of a pass, whole: what it changed, and the state it left."""

    run_index: int
    functions: dict[str, FnChange] = field(default_factory=dict)
    dots: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    src_maps: dict[str, list[Any]] = field(default_factory=dict)


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
    # Every run this card covers, in pipeline order; empty when it ran once.
    runs: list[RunSegment] = field(default_factory=list)
    dots: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    analyses: dict[str, list[str]] = field(default_factory=dict)  # run/cached/invalidated
    log: str = ""
    spills: dict[str, int] = field(default_factory=dict)  # mir only: fn -> count
    # mir only: fn -> the spill/reload sites behind that count.
    spill_sites: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    reg_map: dict[str, dict[str, str]] = field(default_factory=dict)  # mir only
    # mir only, instruction selection alone: fn -> IR/MIR block correlation.
    isel_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    asm: str | None = None  # final assembly text, attached to the last mir pass
    asm_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    is_custom: bool = False  # named via --custom-pass (plugin-loaded pass)
    # Synthetic pre-pipeline card; nothing to diff against, no CFG to show.
    is_input: bool = False
    is_output: bool = False
    # fn -> source map of the "after" snapshot (per line, [file index, line] or None).
    src_maps: dict[str, list[Any]] = field(default_factory=dict)
    # Pass-manager scope (ir lane only): "module" | "cgscc" | "function" | "loop".
    scope: str | None = None


def _functions_json(
    changes: dict[str, FnChange],
    dots: dict[str, tuple[str | None, str | None]],
    src_maps: dict[str, list[Any]],
) -> dict[str, Any]:
    """One set of changes as the frontend reads it: text, graph, source map."""
    out: dict[str, Any] = {}
    for fn, change in changes.items():
        entry: dict[str, Any] = {
            "before": change.before,
            "after": change.after,
            "changed": change.changed,
        }
        dot_before, dot_after = dots.get(fn, (None, None))
        if dot_before:
            entry["dotBefore"] = dot_before
        if dot_after:
            entry["dotAfter"] = dot_after
        # Omit all-None maps to avoid a full-length null array per pass.
        src_after = src_maps.get(fn)
        if src_after and any(src_after):
            entry["srcAfter"] = src_after
        out[fn] = entry
    return out


def _pass_json(pass_: ReportPass) -> dict[str, Any]:
    functions = _functions_json(pass_.functions, pass_.dots, pass_.src_maps)

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
    if len(pass_.runs) > 1:
        entry["runs"] = [
            {"runIndex": run.run_index,
             "functions": _functions_json(run.functions, run.dots, run.src_maps)}
            for run in pass_.runs
        ]
    if pass_.lane == "mir":
        entry["spills"] = pass_.spills
        entry["spillSites"] = pass_.spill_sites
        entry["regMap"] = pass_.reg_map
        entry["iselMap"] = pass_.isel_map
        entry["asm"] = pass_.asm
        entry["asmMap"] = pass_.asm_map
    return entry


def _delta_of(changes: dict[str, FnChange]) -> tuple[int, int]:
    """Lines added and removed across one set of changes, module row winning."""
    module_change = changes.get(MODULE_FN)
    if module_change is not None:
        return module_change.line_delta
    added = removed = 0
    for change in changes.values():
        fn_added, fn_removed = change.line_delta
        added += fn_added
        removed += fn_removed
    return added, removed


def _line_delta(pass_: ReportPass) -> dict[str, int] | None:
    """Lines added/removed across every run and function this pass touched."""
    if pass_.is_input or pass_.is_output:
        return None
    added = removed = 0
    for run in pass_.runs or [None]:
        run_added, run_removed = _delta_of(
            pass_.functions if run is None else run.functions)
        added += run_added
        removed += run_removed
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
            "isOutput": p.is_output,
            "lineDelta": _line_delta(p),
            # Every run this card answers for, so a run can be resolved to a card.
            "runs": [run.run_index for run in p.runs] or [p.run_index],
            "spillCount": sum(p.spills.values()) if p.spills else None,
            # Which functions the ISel view can be offered for, if any.
            "iselFns": sorted(p.isel_map) or None,
            "hasAsm": bool(p.asm),
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


def _ai_config_json(ai_config: dict[str, Any] | None) -> dict[str, Any] | None:
    """The subset of the user config the browser needs, or None to write none."""
    if not ai_config or not ai_config.get("api_key"):
        return None
    return {
        "provider": ai_config.get("provider", "anthropic"),
        "api_key": ai_config["api_key"],
        "model": ai_config.get("model") or "",
        "base_url": ai_config.get("base_url") or "",
    }


def emit_report(
    report_dir: str | Path,
    *,
    passes: list[ReportPass],
    metadata: dict[str, Any],
    frontend_dir: str | Path,
    ai_config: dict[str, Any] | None = None,
    blame: dict[str, dict[str, Any]] | None = None,
    causality: dict[str, Any] | None = None,
) -> Path:
    """Write the report into *report_dir*; returns the manifest path."""
    report_dir = Path(report_dir)
    data_dir = report_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    ai = _ai_config_json(ai_config)
    if ai is not None:
        _write_json_plus_script(data_dir / "ai-config", ai, AI_CONFIG_ASSIGN)
    else:
        for stale in (data_dir / "ai-config.json", data_dir / "ai-config.js"):
            stale.unlink(missing_ok=True)

    # One lineage document per lane; a lane that is gone this build takes its
    # document with it, or a stale one would answer for the new report.
    for lane, document in (blame or {}).items():
        _write_json_plus_script(
            data_dir / f"blame-{lane}", document,
            f'window.__LLVM_LENS_DATA__["blame-{lane}"]',
        )
    for lane in ("ir", "mir"):
        if blame and lane in blame:
            continue
        for stale in (data_dir / f"blame-{lane}.json", data_dir / f"blame-{lane}.js"):
            stale.unlink(missing_ok=True)

    # Same rule as blame: no graph this build means no graph in the report.
    if causality is not None:
        _write_json_plus_script(
            data_dir / "causality", causality,
            'window.__LLVM_LENS_DATA__["causality"]',
        )
    else:
        for stale in (data_dir / "causality.json", data_dir / "causality.js"):
            stale.unlink(missing_ok=True)

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
