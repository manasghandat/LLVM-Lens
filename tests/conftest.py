"""Shared fixtures. Integration tests (real clang/opt/llc runs) skip cleanly
when no LLVM toolchain is discoverable; on this machine LLVM 22 lives in
/usr/lib/llvm-22/bin and is picked up by the fallback candidates below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.toolchain import ToolchainError, discover_toolchain

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
