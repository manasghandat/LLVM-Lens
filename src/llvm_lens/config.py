"""User configuration: the AI provider + key behind the report's "ask AI" panel.

The report is a static file:// page, so the browser cannot read this file
directly. `llvm-lens configure-ai` writes it once, and build_report copies it
into the report's data/ directory as a gitignored sidecar (emit.py), which the
frontend reads on load. The key therefore never reaches index.html, app.js or
manifest.json -- only data/ai-config.*, which is ignored by git.

That sidecar is not mirrored into the browser's own storage. The two copies
stay apart on purpose: `configure-ai --clear` removes this file, and if the
build's copy had also been cached in the browser it would outlive both this
file and the report it came from, leaving the panel answering with a key the
user had removed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The two wire protocols the frontend speaks. "openai-compatible" is a
# configurable base URL, so one entry covers OpenRouter, Ollama, LM Studio,
# vLLM and any proxy in front of OpenAI's own host.
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
    # Only kept when it differs from the built-in default, so the stored file
    # stays minimal and a default change here still reaches the user.
    if base_url and base_url != DEFAULT_BASE_URLS[provider]:
        out["base_url"] = base_url
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        out["api_key"] = api_key
    return out


def load_config(path: Path | None = None) -> dict[str, Any] | None:
    """The stored config, or None when it is missing or unusable.

    Read is deliberately forgiving: a corrupt or half-written file must not
    break a report build, it just means the ask-AI panel arrives unconfigured.
    """
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
    # Create with 0600 from the start: the key must never be readable by
    # another user, not even for the instant between write and chmod.
    fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    os.chmod(file, 0o600)
    return file
