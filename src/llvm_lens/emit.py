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

# opt names a whole-module dump "[module]". A module pass is attributed to the
# functions it rewrote (report.build_lane_a), but the "[module]" pseudo-row is
# also kept as the whole-module diff.
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

# The ask-AI credentials, written beside the manifest rather than into it: the
# manifest is the report's data contract, and a secret does not belong in it.
# Written only when configure-ai has stored a key, and gitignored -- a report
# directory is meant to be shareable, so `--no-ai` must leave nothing behind.
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
    # mir only: fn -> the spill/reload sites behind that count.
    spill_sites: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    reg_map: dict[str, dict[str, str]] = field(default_factory=dict)  # mir only
    # mir only, instruction selection alone: fn -> IR/MIR block correlation.
    isel_map: dict[str, dict[str, Any]] = field(default_factory=dict)
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
        entry["spillSites"] = pass_.spill_sites
        entry["regMap"] = pass_.reg_map
        entry["iselMap"] = pass_.isel_map
        entry["asm"] = pass_.asm
    return entry


def _line_delta(pass_: ReportPass) -> dict[str, int] | None:
    """Lines added/removed across every function this pass touched."""
    if pass_.is_input:
        return None
    module_change = pass_.functions.get(MODULE_FN)
    if module_change is not None:
        # The "[module]" pseudo-row holds the whole module, so its diff already
        # covers every function inside it (plus any module-level edit, e.g. a
        # global initializer). The per-function rows are attributed for the
        # function views; adding them here as well would double count.
        added, removed = module_change.line_delta
        return {"added": added, "removed": removed}
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
            # Which functions the ISel view can be offered for, if any.
            "iselFns": sorted(p.isel_map) or None,
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
) -> Path:
    """Write the report into *report_dir*; returns the manifest path."""
    report_dir = Path(report_dir)
    data_dir = report_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    ai = _ai_config_json(ai_config)
    if ai is not None:
        _write_json_plus_script(data_dir / "ai-config", ai, AI_CONFIG_ASSIGN)
    else:
        # A rebuild into an existing directory must not leave a previous run's
        # credentials behind -- that is exactly the case --no-ai exists for.
        for stale in (data_dir / "ai-config.json", data_dir / "ai-config.js"):
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
