"""Tests for llvm_lens.runner_opt: opt invocation, capture, crash/timeout handling."""

from __future__ import annotations

import pytest

from llvm_lens.runner_opt import OptError, run_opt

# --- end-to-end against a real toolchain ------------------------------------


def test_run_opt_success(toolchain, sample_ir, tmp_path):
    result = run_opt(toolchain, sample_ir, "mem2reg", out_dir=tmp_path)
    assert not result.failed
    assert result.returncode == 0
    assert result.ir_path == tmp_path / "opt-final.ll"

    final = result.ir_path.read_text()
    assert "define" in final
    assert "alloca i64, align 8" in final  # volatile timing read
    assert "alloca ptr" not in final
    assert "alloca i32" not in final

    # stderr carries the instrumentation on LLVM 22
    stderr = result.stderr_path.read_text()
    assert "IR Dump After PromotePass on main" in stderr  # -print-changed=quiet
    assert "Running pass" in stderr  # -debug-pass-manager
    assert "Running analysis" in stderr
    assert "Total Execution Time" in stderr  # -time-passes
    header = stderr.index("IR Dump After PromotePass on main")
    assert stderr[header:].lstrip().splitlines()[1].startswith("; ModuleID = ")


def test_run_opt_unknown_pass_fails(toolchain, sample_ir, tmp_path):
    result = run_opt(toolchain, sample_ir, "no-such-pass", out_dir=tmp_path)
    assert result.failed
    assert result.returncode != 0
    assert not result.timed_out
    assert result.ir_path is None  # no final IR on failure
    assert "unknown pass name" in result.stderr_path.read_text()


def test_run_opt_timeout(toolchain, sample_ir, tmp_path):
    result = run_opt(toolchain, sample_ir, "mem2reg", out_dir=tmp_path, timeout=1e-6)
    assert result.timed_out
    assert result.failed


def test_run_opt_missing_input(toolchain, tmp_path):
    with pytest.raises(OptError, match="input IR not found"):
        run_opt(toolchain, tmp_path / "nope.ll", "mem2reg", out_dir=tmp_path)
