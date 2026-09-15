"""Build settings: the YAML file behind the flags and the report's view defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PASSES = "default<O2>"

# The per-function analysis graphs, and the one module-wide graph.  `all` means
# the six; the call graph is opt-in because it does not vary with the function.
FUNCTION_ANALYSES = ("pdt", "cdg", "ddg", "pdg", "mdg", "lnt")
CALL_GRAPH = "cg"
ALL_ANALYSES = "all"

# Searched upward from the working directory, then the user's own config.
PROJECT_FILENAMES = ("llvm-lens.yml", "llvm-lens.yaml", ".llvm-lens.yml")
USER_CONFIG_PARTS = ("llvm-lens", "config.yml")
SETTINGS_ENV = "LLVM_LENS_SETTINGS"


class SettingsError(Exception):
    """A settings file that cannot be read, parsed, or trusted."""


# --- the schema ---------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One accepted key: its default, its type, and what it does."""

    default: Any
    help: str
    kind: str = "text"  # text | path | bool | int | number | list | enum | enum-list
    choices: tuple[str, ...] = ()


# Nested one level deep. A dict value marks a section; a Field marks a scalar.
SCHEMA: dict[str, Any] = {
    "output": Field(
        "report", "Where to write the report (relative to your shell's cwd).",
        kind="text",
    ),
    "passes": Field(
        DEFAULT_PASSES, "New-PM pipeline string for opt (Lane A).", kind="text",
    ),
    "custom-passes": Field(
        [], "Custom function-pass names: appended as function(<name>) and badged.",
        kind="list",
    ),
    "source-map": Field(
        True, "Correlate IR/MIR lines with the source (needs debug info).",
        kind="bool",
    ),
    "open": Field(False, "Open the report in a browser once it is written.", kind="bool"),
    "ai": Field(
        True, "Copy the stored AI credentials into the report (see configure-ai).",
        kind="bool",
    ),
    "timeout": Field(
        None, "Per-tool timeout in seconds. Null means no limit.", kind="number",
    ),
    "llvm": {
        "bin-dir": Field(None, "Directory holding the LLVM tools.", kind="path"),
        "version": Field(None, "Expected LLVM major version.", kind="int"),
        "target": Field(
            None, "Target triple, passed to clang --target and llc -mtriple.",
            kind="text",
        ),
    },
    "plugins": {
        "dir": Field(
            None, "Directory that bare plugin names in this file resolve against.",
            kind="path",
        ),
        "pass": Field(
            [], "New-PM pass plugin .so files to load (opt and llc).", kind="list",
        ),
        "legacy": Field(
            [], "Legacy plugin .so files to load (llc backend only).", kind="list",
        ),
    },
    "flags": {
        "clang": Field([], "Extra arguments appended to the clang command.", kind="list"),
        "opt": Field([], "Extra arguments appended to the opt command.", kind="list"),
        "llc": Field([], "Extra arguments appended to the llc command.", kind="list"),
    },
    "ui": {
        "lane": Field("ir", "Lane open on load.", kind="enum", choices=("ir", "mir")),
        "mode": Field(
            "diff", "View open on load.",
            kind="enum",
            choices=("cfg", "diff", "ir", "blame", "src", "isel", "analyses",
                     "structure", "pipeline"),
        ),
        "analysis": Field(
            ("pdt",), "Graphs view: which analysis graphs to draw (a list, or 'all').",
            kind="enum-list", choices=FUNCTION_ANALYSES + (CALL_GRAPH,),
        ),
        "orientation": Field(
            "side", "Split direction for the two panes.",
            kind="enum", choices=("side", "stack"),
        ),
        "split-ratio": Field(0.5, "First pane's share of the split, 0.15-0.85.", kind="number"),
        "drawer": Field(True, "Whether the detail drawer starts open.", kind="bool"),
        "drawer-tab": Field(
            "Log", "Drawer tab open on load (falls back to Log when absent).", kind="text",
        ),
        "changed-only": Field(
            False, "Start with the pass list filtered to changed passes.", kind="bool",
        ),
        "flow-both-lanes": Field(
            True, "Flow view: draw both lanes, or only the selected one.", kind="bool",
        ),
    },
}


def _flat_spec() -> dict[str, Field]:
    """The schema keyed by dotted path: ``{"ui.lane": Field(...)}``."""
    flat: dict[str, Field] = {}
    for key, spec in SCHEMA.items():
        if isinstance(spec, dict):
            for sub, sub_spec in spec.items():
                flat[f"{key}.{sub}"] = sub_spec
        else:
            flat[key] = spec
    return flat


FLAT_SPEC = _flat_spec()


# --- the parsed result --------------------------------------------------------


@dataclass(frozen=True)
class Ui:
    """The report viewer's starting state (see STATE in frontend/app.js)."""

    lane: str = "ir"
    mode: str = "diff"
    analysis: tuple[str, ...] = ("pdt",)
    orientation: str = "side"
    split_ratio: float = 0.5
    drawer: bool = True
    drawer_tab: str = "Log"
    changed_only: bool = False
    flow_both_lanes: bool = True

    def as_metadata(self) -> dict[str, Any]:
        """The camelCase shape frontend/app.js reads off the manifest."""
        return {
            "lane": self.lane,
            "mode": self.mode,
            "analysis": list(self.analysis),
            "orientation": self.orientation,
            "splitRatio": self.split_ratio,
            "drawer": self.drawer,
            "drawerTab": self.drawer_tab,
            "changedOnly": self.changed_only,
            "flowBothLanes": self.flow_both_lanes,
        }


@dataclass(frozen=True)
class Settings:
    """Every knob, at its default until a config file or a flag says otherwise."""

    output: str = "report"
    passes: str = DEFAULT_PASSES
    custom_passes: tuple[str, ...] = ()
    pass_plugins: tuple[str, ...] = ()
    legacy_plugins: tuple[str, ...] = ()
    plugin_dir: str | None = None
    bin_dir: str | None = None
    llvm_version: int | None = None
    target: str | None = None
    timeout: float | None = None
    source_map: bool = True
    open_report: bool = False
    ai: bool = True
    clang_args: tuple[str, ...] = ()
    opt_args: tuple[str, ...] = ()
    llc_args: tuple[str, ...] = ()
    ui: Ui = field(default_factory=Ui)
    # The file these came from, for the manifest and the error messages.
    config_file: str | None = None

    def overridden(self, **fields: Any) -> Settings:
        """Apply a set of values; ``None`` means "the flag was not given"."""
        return replace(self, **{k: v for k, v in fields.items() if v is not None})


# --- reading a file -----------------------------------------------------------


def _user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path("~/.config").expanduser()
    return root.joinpath(*USER_CONFIG_PARTS)


def find_config(start: str | Path | None = None) -> Path | None:
    """The config to use: LLVM_LENS_SETTINGS, else the nearest file, else the user's."""
    override = os.environ.get(SETTINGS_ENV)
    if override:
        return Path(override).expanduser()
    directory = Path(start).expanduser().resolve() if start else Path.cwd()
    if directory.is_file():
        directory = directory.parent
    for candidate in (directory, *directory.parents):
        for name in PROJECT_FILENAMES:
            path = candidate / name
            if path.is_file():
                return path
    user = _user_config_path()
    return user if user.is_file() else None


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SettingsError(f"{path} is not valid YAML:\n{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SettingsError(
            f"{path}: the top level must be a mapping of settings, "
            f"not {type(raw).__name__}"
        )
    return raw


def _coerce_enum_list(where: str, value: Any, spec: Field) -> tuple[str, ...]:
    """Names from *spec.choices*, or the ``all`` shorthand, in canonical order."""
    if isinstance(value, str):
        picked = list(FUNCTION_ANALYSES) if value == ALL_ANALYSES else [value]
    elif isinstance(value, (list, tuple)):
        picked = list(value)
    else:
        raise SettingsError(f"{where} must be a list (got {value!r})")
    if not picked:
        raise SettingsError(f"{where} must name at least one graph")
    for item in picked:
        if item not in spec.choices:
            raise SettingsError(
                f"{where} must be one of {', '.join(spec.choices)} "
                f"(or {ALL_ANALYSES}); got {item!r}"
            )
    # Canonical order, so the pane and the chip row always agree.
    return tuple(name for name in spec.choices if name in set(picked))


def _coerce(key: str, value: Any, spec: Field, path: Path | None = None) -> Any:
    """One setting, converted to the type its kind calls for."""
    where = f"{path}: {key}" if path is not None else key
    if spec.kind == "enum":
        if value not in spec.choices:
            raise SettingsError(
                f"{where} must be one of {', '.join(spec.choices)} (got {value!r})"
            )
        return value
    if value is None:
        return spec.default
    if spec.kind == "enum-list":
        return _coerce_enum_list(where, value, spec)
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{where} must be true or false (got {value!r})")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsError(f"{where} must be a whole number (got {value!r})")
        return value
    if spec.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{where} must be a number (got {value!r})")
        return float(value)
    if spec.kind == "list":
        if isinstance(value, str):
            return (value,)
        if not isinstance(value, (list, tuple)):
            raise SettingsError(f"{where} must be a list (got {value!r})")
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise SettingsError(f"{where} must be a list of strings (got {item!r})")
        return tuple(str(item) for item in value)
    if isinstance(value, (list, dict)):
        raise SettingsError(f"{where} must be a single value (got {type(value).__name__})")
    return str(value)


def _resolve(base: Path | None, value: str | None) -> str | None:
    """A path written in a config file is relative to that file, not to cwd."""
    if value is None or base is None:
        return value
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def _flatten(raw: dict[str, Any], path: Path) -> dict[str, Any]:
    """Nest -> dotted keys, rejecting anything the schema does not know."""
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        spec = SCHEMA.get(key)
        if spec is None:
            raise SettingsError(_unknown(path, key))
        if isinstance(spec, dict):
            # `llvm:` with every entry commented out is an empty section, not
            # a mistake: it reads as "nothing to override here".
            if value is None:
                continue
            if not isinstance(value, dict):
                raise SettingsError(
                    f"{path}: {key!r} is a section and must be a mapping "
                    f"of settings (got {type(value).__name__})"
                )
            for sub, sub_value in value.items():
                if sub not in spec:
                    raise SettingsError(_unknown(path, f"{key}.{sub}"))
                flat[f"{key}.{sub}"] = _coerce(f"{key}.{sub}", sub_value, spec[sub], path)
        else:
            flat[key] = _coerce(key, value, spec, path)
    return flat


def _unknown(path: Path, key: str) -> str:
    near = [name for name in FLAT_SPEC if key in name or name in key]
    hint = f"; did you mean {', '.join(sorted(near)[:3])}?" if near else ""
    return (
        f"{path}: unknown setting {key!r}{hint}\n"
        f"(`llvm-lens --init-config` writes the full list)"
    )


def _plugin_paths(flat: dict[str, Any], base: Path | None) -> dict[str, Any]:
    """Resolve plugin paths: against plugins.dir when bare, else the config's dir."""
    plugin_dir = _resolve(base, flat.get("plugins.dir"))
    out: dict[str, Any] = {"plugin_dir": plugin_dir}
    for key, into in (("plugins.pass", "pass_plugins"), ("plugins.legacy", "legacy_plugins")):
        resolved = []
        for entry in flat.get(key, ()):
            path = Path(entry).expanduser()
            if path.is_absolute():
                resolved.append(str(path))
            elif plugin_dir and path.parent == Path("."):
                resolved.append(str(Path(plugin_dir) / path))
            else:
                resolved.append(_resolve(base, entry))
        out[into] = tuple(resolved)
    return out


def _build(flat: dict[str, Any], path: Path | None, origin: str | None) -> Settings:
    def value(key: str) -> Any:
        if key in flat:
            return flat[key]  # _flatten already coerced it
        # Absent keys still go through _coerce: a schema default is a plain
        # Python value, while callers expect the type its kind promises.
        return _coerce(key, FLAT_SPEC[key].default, FLAT_SPEC[key])

    base = path.parent if path is not None else None
    plugins = _plugin_paths(flat, base)
    return Settings(
        output=value("output"),
        passes=value("passes"),
        custom_passes=value("custom-passes"),
        pass_plugins=plugins["pass_plugins"],
        legacy_plugins=plugins["legacy_plugins"],
        plugin_dir=plugins["plugin_dir"],
        bin_dir=_resolve(base, value("llvm.bin-dir")),
        llvm_version=value("llvm.version"),
        target=value("llvm.target"),
        timeout=value("timeout"),
        source_map=value("source-map"),
        open_report=value("open"),
        ai=value("ai"),
        clang_args=value("flags.clang"),
        opt_args=value("flags.opt"),
        llc_args=value("flags.llc"),
        ui=Ui(
            lane=value("ui.lane"),
            mode=value("ui.mode"),
            analysis=value("ui.analysis"),
            orientation=value("ui.orientation"),
            split_ratio=value("ui.split-ratio"),
            drawer=value("ui.drawer"),
            drawer_tab=value("ui.drawer-tab"),
            changed_only=value("ui.changed-only"),
            flow_both_lanes=value("ui.flow-both-lanes"),
        ),
        config_file=origin,
    )


def load_settings(path: str | Path | None = None) -> Settings:
    """Read *path*, or the config `find_config` picks, else pure defaults."""
    chosen: Path | None
    if path is not None:
        chosen = Path(path).expanduser()
        if not chosen.is_file():
            raise SettingsError(f"no such config file: {chosen}")
    else:
        chosen = find_config()
        if chosen is None:
            return Settings()
        if not chosen.is_file():
            raise SettingsError(
                f"{SETTINGS_ENV} points at {chosen}, which does not exist"
            )
    return _build(_flatten(_read(chosen), chosen), chosen, str(chosen))


# --- writing a starter file ---------------------------------------------------


def _scalar(value: Any, indent: str) -> str:
    """One YAML value, ready to sit after a `key:`."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # Dumped as a one-key mapping: a bare scalar would gain a "..." marker.
        return yaml.safe_dump({"v": value}, default_flow_style=False).split(": ", 1)[1].rstrip("\n")
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        dumped = yaml.safe_dump(list(value), default_flow_style=False, sort_keys=False)
        body = dumped.rstrip("\n").split("\n")
        return "\n" + "\n".join(f"{indent}  {line}" for line in body)
    return yaml.safe_dump(value, default_flow_style=True).rstrip("\n")


def template() -> str:
    """A commented starter config: every key, at the value it already has."""
    lines = [
        "# LLVM-Lens build settings.",
        "#",
        "# Every key is optional -- the value shown is what you get without it, and",
        "# a command-line flag always wins over this file.  Paths written here",
        "# (llvm.bin-dir, plugins.dir, plugin entries) are relative to this file's",
        "# directory, so it can be committed next to the code it analyzes;",
        "# `output` is the exception and stays relative to your shell.",
        "",
    ]
    for key, spec in SCHEMA.items():
        if not isinstance(spec, dict):
            lines.append(f"# {spec.help}")
            lines.append(f"{key}: {_scalar(spec.default, '')}")
            lines.append("")
            continue
        lines.append(f"{key}:")
        for sub, sub_spec in spec.items():
            lines.append(f"  # {sub_spec.help}")
            lines.append(f"  {sub}: {_scalar(sub_spec.default, '  ')}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_template(path: str | Path) -> Path:
    """Write `template()` to *path*, refusing to clobber an existing file."""
    target = Path(path).expanduser()
    if target.exists():
        raise SettingsError(f"{target} already exists; not overwriting it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template(), encoding="utf-8")
    return target
