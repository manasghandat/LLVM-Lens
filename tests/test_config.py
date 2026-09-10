"""The ask-AI configuration: ~/.llvm_lens_config, `llvm-lens configure-ai`,
and the data/ai-config.* sidecar the report is built with.

The key is a credential, so most of these tests are about where it does *not*
go: never into the manifest, never into the report's HTML or JS, never printed
back at the user, and never left behind by a rebuild that asked for no AI.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from llvm_lens import config as config_mod
from llvm_lens.cli import main
from llvm_lens.config import (
    ConfigError, config_path, load_config, normalize, save_config,
)
from llvm_lens.emit import emit_report
from llvm_lens.report import build_report
from tests.conftest import SAMPLE_C

FRONTEND = Path(__file__).parent.parent / "src" / "llvm_lens" / "frontend"
KEY = "sk-ant-api03-notarealkey1234"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the whole module at a throwaway config file, not the real home."""
    path = tmp_path / "cfg" / ".llvm_lens_config"
    monkeypatch.setenv(config_mod.CONFIG_ENV, str(path))
    return path


# --- the stored file ------------------------------------------------------------


def test_save_config_round_trips(home):
    written = save_config({"provider": "anthropic", "api_key": KEY,
                           "model": "claude-opus-5"})
    assert written == home
    assert json.loads(home.read_text())["api_key"] == KEY
    assert load_config() == {"provider": "anthropic", "model": "claude-opus-5",
                             "api_key": KEY}


def test_saved_config_is_readable_only_by_its_owner(home):
    save_config({"provider": "anthropic", "api_key": KEY})
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o600


def test_save_config_creates_its_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "deeper" / "cfg"
    assert save_config({"provider": "anthropic", "api_key": KEY},
                       path=nested / "config").is_file()


def test_load_config_is_forgiving(tmp_path):
    """A report build must never fail on a missing or broken config."""
    assert load_config(tmp_path / "absent") is None

    corrupt = tmp_path / "corrupt"
    corrupt.write_text("{not json")
    assert load_config(corrupt) is None

    not_object = tmp_path / "list"
    not_object.write_text('["anthropic"]')
    assert load_config(not_object) is None

    # A provider the frontend cannot speak means "unconfigured", not a crash.
    unknown = tmp_path / "unknown"
    unknown.write_text('{"provider": "gemini", "api_key": "x"}')
    assert load_config(unknown) is None


def test_normalize_fills_defaults_and_drops_what_is_not_set():
    assert normalize({}) == {"provider": "anthropic", "model": "claude-opus-5"}
    assert normalize({"provider": "openai-compatible"}) == {"provider": "openai-compatible"}
    # The built-in base URL is implied, so storing it would freeze today's
    # default into the file; a non-default one is the whole point of the field.
    assert "base_url" not in normalize({"provider": "anthropic",
                                        "base_url": "https://api.anthropic.com"})
    assert normalize({"provider": "openai-compatible",
                      "base_url": "http://localhost:11434/v1/"})["base_url"] == \
        "http://localhost:11434/v1/"
    assert "api_key" not in normalize({"provider": "anthropic", "api_key": "  "})
    assert normalize({"provider": "anthropic", "api_key": f"  {KEY}  "})["api_key"] == KEY


def test_normalize_rejects_a_provider_the_frontend_cannot_speak():
    with pytest.raises(ConfigError):
        normalize({"provider": "gemini", "api_key": KEY})


def test_the_suite_never_reads_the_developer_s_own_config():
    """conftest points every test at a path that cannot exist.

    build_report() reads the stored config to decide whether to write the
    report's ai-config sidecar, so a test that forgets to pass ai_config would
    otherwise copy the developer's real key into a temp directory.
    """
    assert config_path() != config_mod.DEFAULT_CONFIG_PATH
    assert not config_path().exists()
    assert load_config() is None


def test_config_path_follows_the_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv(config_mod.CONFIG_ENV, str(tmp_path / "elsewhere"))
    assert config_path() == tmp_path / "elsewhere"
    monkeypatch.delenv(config_mod.CONFIG_ENV)
    assert config_path().name == ".llvm_lens_config"


# --- the configure-ai command ---------------------------------------------------


def test_configure_ai_stores_the_key_without_echoing_it(home, capsys):
    assert main(["configure-ai", "--provider", "anthropic", "--api-key", KEY,
                 "--model", "claude-opus-5"]) == 0
    out = capsys.readouterr().out
    assert KEY not in out
    assert "••••••••1234" in out  # masked to the last four characters
    assert str(home) in out
    assert load_config()["api_key"] == KEY


def test_configure_ai_show_never_prints_the_key(home, capsys):
    save_config({"provider": "anthropic", "api_key": KEY, "model": "claude-opus-5"})
    capsys.readouterr()
    assert main(["configure-ai", "--show"]) == 0
    out = capsys.readouterr().out
    assert KEY not in out
    assert "••••••••1234" in out
    assert "anthropic" in out and "claude-opus-5" in out


def test_configure_ai_show_says_so_when_nothing_is_stored(home, capsys):
    assert main(["configure-ai", "--show"]) == 1
    assert "no configuration" in capsys.readouterr().out


def test_configure_ai_clear_removes_the_file(home, capsys):
    save_config({"provider": "anthropic", "api_key": KEY})
    assert main(["configure-ai", "--clear"]) == 0
    assert not home.exists()
    # Clearing twice is not an error -- the goal state is reached either way.
    assert main(["configure-ai", "--clear"]) == 0


def test_configure_ai_clear_says_what_it_could_not_reach(home, capsys):
    """The file is not the only copy of the key, and --clear looked broken
    because of it: reports built earlier carry their own copy, and a key typed
    into the panel lives in that browser. No command-line tool can remove
    either, so the command has to name them instead of just reporting success.
    """
    save_config({"provider": "anthropic", "api_key": KEY})
    capsys.readouterr()
    main(["configure-ai", "--clear"])
    out = capsys.readouterr().out
    assert KEY not in out
    assert "--no-ai" in out          # how to drop a report's own copy
    assert "forget" in out           # how to drop the browser's


def test_configure_ai_survives_being_run_without_a_key(home, capsys):
    """Storing a provider but no key is allowed; the panel just stays off."""
    assert main(["configure-ai", "--provider", "openai-compatible",
                 "--model", "qwen3", "--base-url", "http://localhost:11434/v1"]) == 0
    assert "warning" in capsys.readouterr().err
    stored = load_config()
    assert "api_key" not in stored
    assert stored["base_url"] == "http://localhost:11434/v1"


def test_configure_ai_is_dispatched_before_the_build_parser(home, capsys):
    """`llvm-lens configure-ai` has no source file, and must not need one."""
    assert main(["--configure-ai", "--show"]) == 1  # the alias, unconfigured
    assert "no configuration" in capsys.readouterr().out


# --- the sidecar the browser reads ----------------------------------------------


def _emit(report_dir: Path, ai_config):
    return emit_report(report_dir, passes=[], metadata={},
                       frontend_dir=FRONTEND, ai_config=ai_config)


def test_sidecar_is_written_only_when_a_key_is_stored(tmp_path):
    ai = {"provider": "openai-compatible", "api_key": KEY, "model": "qwen3",
          "base_url": "http://localhost:11434/v1"}
    _emit(tmp_path / "report", ai)
    data = tmp_path / "report" / "data"
    assert json.loads((data / "ai-config.json").read_text()) == \
        {"provider": "openai-compatible", "api_key": KEY, "model": "qwen3",
         "base_url": "http://localhost:11434/v1"}
    # The file:// fallback: a plain assignment to the global the frontend reads.
    assert "window.__LLVM_LENS_AI_CONFIG__" in (data / "ai-config.js").read_text()

    # No key -> no sidecar, whatever else the config says.
    for index, empty in enumerate([None, {}, {"provider": "anthropic",
                                             "model": "claude-opus-5"}]):
        _emit(tmp_path / f"bare-{index}", empty)
        assert not (tmp_path / f"bare-{index}" / "data" / "ai-config.json").exists()


def test_a_rebuild_without_ai_leaves_no_credentials_behind(tmp_path):
    """The whole point of --no-ai: rebuilding into a shared directory."""
    report = tmp_path / "report"
    _emit(report, {"provider": "anthropic", "api_key": KEY})
    assert (report / "data" / "ai-config.json").exists()

    _emit(report, {})
    assert not (report / "data" / "ai-config.json").exists()
    assert not (report / "data" / "ai-config.js").exists()
    # ...and the rest of the report is still there.
    assert (report / "data" / "manifest.json").exists()


def test_the_key_never_reaches_the_report_s_own_files(tmp_path):
    report = tmp_path / "report"
    _emit(report, {"provider": "anthropic", "api_key": KEY})
    for name in ("index.html", "app.js", "style.css"):
        assert KEY not in (report / name).read_text()
    assert KEY not in (report / "data" / "manifest.json").read_text()
    assert KEY not in (report / "data" / "manifest.js").read_text()


def test_build_report_embeds_and_then_drops_the_credentials(home, toolchain, tmp_path):
    """The real path: configure once, build, then rebuild for sharing."""
    save_config({"provider": "anthropic", "api_key": KEY, "model": "claude-opus-5"})
    out = tmp_path / "report"

    summary = build_report(SAMPLE_C, passes="mem2reg", output=out,
                           bin_dir=toolchain.bin_dir, source_map=False)
    assert summary["aiEmbedded"] is True
    assert json.loads((out / "data" / "ai-config.json").read_text())["api_key"] == KEY

    # build_report resolves the stored config itself when not told otherwise,
    # so the CLI's --no-ai is the only thing that has to pass {} explicitly.
    shared = build_report(SAMPLE_C, passes="mem2reg", output=out,
                          bin_dir=toolchain.bin_dir, source_map=False, ai_config={})
    assert shared["aiEmbedded"] is False
    assert not (out / "data" / "ai-config.json").exists()
