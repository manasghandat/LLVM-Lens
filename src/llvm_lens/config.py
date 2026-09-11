'''User configuration: the AI provider + key behind the report's "ask AI" panel.'''

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PROVIDERS = ("anthropic", "openai-compatible")

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai-compatible": "",
}

DEFAULT_BASE_URLS = {
    "anthropic": "https://api.anthropic.com",
    "openai-compatible": "https://api.openai.com/v1",
}

DEFAULT_CONFIG_PATH = Path("~/.llvm_lens_config")
CONFIG_ENV = "LLVM_LENS_CONFIG"


class ConfigError(Exception):
    """A configuration the caller asked to save is not usable."""


def config_path() -> Path:
    """Where the config lives: LLVM_LENS_CONFIG, else ~/.llvm_lens_config."""
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH.expanduser()


def normalize(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill provider defaults and drop blank optional fields."""
    provider = cfg.get("provider") or "anthropic"
    if provider not in PROVIDERS:
        raise ConfigError(
            f"unknown provider {provider!r} (choose from {', '.join(PROVIDERS)})"
        )
    out: dict[str, Any] = {"provider": provider}
    model = (cfg.get("model") or "").strip() or DEFAULT_MODELS[provider]
    if model:
        out["model"] = model
    base_url = (cfg.get("base_url") or "").strip()
    if base_url and base_url != DEFAULT_BASE_URLS[provider]:
        out["base_url"] = base_url
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        out["api_key"] = api_key
    return out


def load_config(path: Path | None = None) -> dict[str, Any] | None:
    """The stored config, or None when it is missing or unusable."""
    file = path or config_path()
    try:
        raw = json.loads(file.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return normalize(raw)
    except ConfigError:
        return None


def save_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    """Write the config 0600 and return where it landed."""
    data = normalize(cfg)
    file = path or config_path()
    file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    os.chmod(file, 0o600)
    return file
