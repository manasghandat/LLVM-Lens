"""Shared fixtures: integration tests skip cleanly without a toolchain."""

from __future__ import annotations

from pathlib import Path

import pytest

from llvm_lens import config as config_mod
from llvm_lens import settings as settings_mod
from llvm_lens.compile import compile_to_ir
from llvm_lens.toolchain import ToolchainError, discover_toolchain

SAMPLE_C = Path(__file__).resolve().parent.parent / "examples" / "side-channel" / "sample.c"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_real_ai_config(tmp_path, monkeypatch):
    """Keep every test off the developer's own ~/.llvm_lens_config."""
    monkeypatch.setenv(config_mod.CONFIG_ENV, str(tmp_path / "no-such-config"))


@pytest.fixture(autouse=True)
def no_real_settings(tmp_path, monkeypatch):
    """...and off any llvm-lens.yml in their tree, or their user config.

    An empty file rather than a missing one: a named-but-absent path is an
    error, while an empty file is exactly "nothing is configured".
    """
    empty = tmp_path / "no-settings.yml"
    empty.write_text("")
    monkeypatch.setenv(settings_mod.SETTINGS_ENV, str(empty))

# Common apt.llvm.org locations, newest first, used when PATH discovery fails.
_BIN_DIR_CANDIDATES = [
    "/usr/lib/llvm-22/bin",
    "/usr/lib/llvm-21/bin",
    "/usr/lib/llvm-20/bin",
    "/usr/lib/llvm-19/bin",
    "/usr/lib/llvm-18/bin",
]


@pytest.fixture(scope="session")
def toolchain():
    try:
        return discover_toolchain()
    except ToolchainError:
        pass
    for candidate in _BIN_DIR_CANDIDATES:
        if Path(candidate).is_dir():
            try:
                return discover_toolchain(bin_dir=candidate)
            except ToolchainError:
                continue
    pytest.skip("no usable LLVM toolchain found (set LLVM_LENS_BIN_DIR)")


@pytest.fixture(scope="session")
def capture():
    """Read a canned opt/llc capture from tests/fixtures/, or skip."""
    def read(name: str) -> str:
        path = FIXTURES / name
        if not path.is_file():
            pytest.skip(f"missing capture fixture: {path}")
        return path.read_text()
    return read


@pytest.fixture(scope="session")
def sample_ir(toolchain, tmp_path_factory):
    """SAMPLE_C compiled to textual IR: the input the runner tests run on."""
    result = compile_to_ir(SAMPLE_C, out_dir=tmp_path_factory.mktemp("sample-ir"),
                           toolchain=toolchain)
    assert result.kind == "clang"
    return result.ir_path
