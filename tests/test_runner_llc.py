"""Tests for cli.runner_llc: llc invocation, capture, crash/timeout handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.runner_llc import LlcError, llc_command, run_llc

FIXTURES = Path(__file__).parent / "fixtures"
OPT_FINAL = FIXTURES / "opt-final-sample.ll"


def test_llc_command_flags(tmp_path):
    cmd = llc_command(
        Path("/llvm/llc"), Path("in.ll"),
        out=tmp_path / "out.s",
        mtriple="aarch64-linux-gnu",
        extra_args=("-stop-after=x86-isel",),
    )
    assert cmd == [
        "/llvm/llc", "-mtriple=aarch64-linux-gnu",
        "-print-after-all", "-debug-pass=Structure", "-time-passes",
        "-stop-after=x86-isel",
        "-o", str(tmp_path / "out.s"), "in.ll",
    ]


def test_llc_command_no_s_flag():  # llc emits asm by default; -S is an error
    cmd = llc_command(Path("/llvm/llc"), Path("in.ll"), out=Path("out.s"))
    assert "-S" not in cmd


def test_run_llc_success(toolchain, tmp_path):
    result = run_llc(OPT_FINAL, out_dir=tmp_path, toolchain=toolchain)
    assert not result.failed
    assert result.returncode == 0
    assert result.asm_path == tmp_path / "final.s"
    asm = result.asm_path.read_text()
    assert "main:" in asm
    assert ".text" in asm

    stderr = result.stderr_path.read_text()
    assert "# Machine code for function main" in stderr  # -print-after-all
    assert "Pass Arguments:" in stderr  # -debug-pass=Structure
    assert "Total Execution Time" in stderr  # -time-passes


def test_run_llc_mtriple(toolchain, tmp_path):
    result = run_llc(
        OPT_FINAL, out_dir=tmp_path, mtriple="aarch64-linux-gnu",
        toolchain=toolchain,
    )
    assert not result.failed
    assert "aarch64" in result.stderr_path.read_text()  # triple in dumps
    assert ".section" in result.asm_path.read_text()


def test_run_llc_bad_flag_fails(toolchain, tmp_path):
    result = run_llc(
        OPT_FINAL, out_dir=tmp_path,
        extra_args=("-no-such-flag-xyz",), toolchain=toolchain,
    )
    assert result.failed
    assert result.asm_path is None


def test_run_llc_timeout(toolchain, tmp_path):
    result = run_llc(OPT_FINAL, out_dir=tmp_path, timeout=1e-6, toolchain=toolchain)
    assert result.timed_out
    assert result.failed


def test_run_llc_missing_input(tmp_path):
    with pytest.raises(LlcError, match="input IR not found"):
        run_llc(tmp_path / "nope.ll", out_dir=tmp_path)
