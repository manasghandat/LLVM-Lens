"""Shared fixtures. Integration tests (real clang/opt/llc runs) skip cleanly
when no LLVM toolchain is discoverable; on this machine LLVM 22 lives in
/usr/lib/llvm-22/bin and is picked up by the fallback candidates below.

The parser tests read canned tool output from tests/fixtures/ instead of
running anything. Those captures are large and are not currently checked in,
so `capture` skips rather than fails when one is absent: they guard exact
LLVM output formats, and a missing capture says nothing about the code under
test. Regenerate one by running the stage that produces it and copying the
matching raw/*.log out of the report directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llvm_lens.compile import compile_to_ir
from llvm_lens.toolchain import ToolchainError, discover_toolchain

# The one source the integration tests compile. It doubles as the --custom-pass
# demo input, which is why it lives under examples/ rather than in fixtures/.
SAMPLE_C = Path(__file__).resolve().parent.parent / "examples" / "side-channel" / "sample.c"
FIXTURES = Path(__file__).parent / "fixtures"

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
