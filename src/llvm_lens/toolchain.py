"""LLVM tool discovery and version sanity checks."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAJOR = 22
BIN_DIR_ENV = "LLVM_LENS_BIN_DIR"

TOOLS = ("clang", "opt", "llc", "llvm-dis")

_VERSION_RE = re.compile(r"version (\d+)\.(\d+)\.(\d+)")


class ToolchainError(RuntimeError):
    """Missing, unexecutable, or version-mismatched LLVM tool."""

@dataclass(frozen=True)
class Tool:
    name: str
    path: Path
    version: str  # first line of `--version` output
    major: int    # parsed major version


@dataclass(frozen=True)
class Toolchain:
    bin_dir: Path | None
    expected_major: int
    tools: dict[str, Tool]

    def __getattr__(self, name: str) -> Tool:
        # Accept both toolchain.llvm-dis and toolchain.llvm_dis.
        if name in TOOLS:
            return self.tools[name]
        if name.replace("_", "-") in TOOLS:
            return self.tools[name.replace("_", "-")]
        raise AttributeError(name)


def _tool_version(name: str, path: Path) -> Tool:
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolchainError(f"cannot run {path}: {exc}") from exc
    if proc.returncode != 0:
        raise ToolchainError(
            f"{path} --version failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    match = _VERSION_RE.search(first_line)
    if not match:
        raise ToolchainError(f"cannot parse LLVM version from {path}: {first_line!r}")
    return Tool(name=name, path=path, version=first_line, major=int(match.group(1)))


def _find_tool(name: str, bin_dir: Path | None, major: int) -> Path:
    if bin_dir is not None:
        path = bin_dir / name
        if not path.is_file():
            raise ToolchainError(f"{name} not found in bin dir {bin_dir} (expected {path})")
        return path
    path = shutil.which(f"{name}-{major}")
    if path is None:
        path = shutil.which(name)
    if path is None:
        raise ToolchainError(
            f"{name} not found on PATH; install LLVM {major}, pass --bin-dir "
            f"(or set {BIN_DIR_ENV}) pointing at its bin dir (e.g. "
            f"/usr/lib/llvm-{major}/bin on Debian/Ubuntu apt.llvm.org "
            f"installs), or select another major with --llvm-version"
        )
    return Path(path)


def discover_toolchain(
    bin_dir: str | Path | None = None,
    expected_major: int | None = None,
) -> Toolchain:
    """Locate clang/opt/llc/llvm-dis and verify they match the expected major."""
    if expected_major is None:
        expected_major = DEFAULT_MAJOR
    if bin_dir is None and os.environ.get(BIN_DIR_ENV):
        bin_dir = Path(os.environ[BIN_DIR_ENV])
    elif bin_dir is not None:
        bin_dir = Path(bin_dir)

    tools: dict[str, Tool] = {}
    for name in TOOLS:
        path = _find_tool(name, bin_dir, expected_major)
        tool = _tool_version(name, path)
        if tool.major != expected_major:
            raise ToolchainError(
                f"{name} is LLVM {tool.major}, expected {expected_major}: {tool.version}\n"
                f"pass --llvm-version {tool.major}, or point --bin-dir "
                f"({BIN_DIR_ENV}) at a matching LLVM {expected_major} installation"
            )
        tools[name] = tool
    return Toolchain(bin_dir=bin_dir, expected_major=expected_major, tools=tools)
