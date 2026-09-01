"""Tests for cli.runner_opt: opt invocation, capture, crash/timeout handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.compile import compile_to_ir
from cli.runner_opt import OptError, opt_command, run_opt

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_C = FIXTURES / "sample.c"


@pytest.fixture(scope="module")
def sample_ir(toolchain, tmp_path_factory):
    out = tmp_path_factory.mktemp("opt-input")
    result = compile_to_ir(SAMPLE_C, out_dir=out, toolchain=toolchain)
    assert result.kind == "clang"
    return result.ir_path


# --- command construction (pure) --------------------------------------------


def test_opt_command_flags(tmp_path):
    cmd = opt_command(
        Path("/llvm/opt"), Path("in.ll"), "default<O2>",
        out=tmp_path / "final.ll",
        mtriple="x86_64",
        load_pass_plugins=("a.so", "b.so"),
        print_after=("my-pass", "other-pass"),
        extra_args=("-debug-only=loop-vectorize",),
    )
    assert cmd == [
        "/llvm/opt", "-S", "-mtriple=x86_64", "-passes=default<O2>",
        "-load-pass-plugin=a.so", "-load-pass-plugin=b.so",
        "-print-changed=quiet", "-debug-pass-manager", "-time-passes",
        "-print-after=my-pass,other-pass",
        "-debug-only=loop-vectorize",
        "-o", str(tmp_path / "final.ll"), "in.ll",
    ]


def test_opt_command_minimal(tmp_path):
    cmd = opt_command(Path("/llvm/opt"), Path("in.ll"), "mem2reg", out=tmp_path / "f.ll")
    assert cmd == [
        "/llvm/opt", "-S", "-passes=mem2reg",
        "-print-changed=quiet", "-debug-pass-manager", "-time-passes",
        "-o", str(tmp_path / "f.ll"), "in.ll",
    ]


# --- end-to-end against a real toolchain ------------------------------------


def test_run_opt_success(toolchain, sample_ir, tmp_path):
    result = run_opt(sample_ir, "mem2reg", out_dir=tmp_path, toolchain=toolchain)
    assert not result.failed
    assert result.returncode == 0
    assert result.ir_path == tmp_path / "opt-final.ll"

    final = result.ir_path.read_text()
    assert "define" in final
    # mem2reg promotes the promotable allocas; volatile and aggregate allocas
    # (getTime's volatile `a`, main's cpu_set_t / flag[]) correctly survive.
    assert "alloca i64, align 8" in final  # volatile timing read
    assert "alloca ptr" not in final
    assert "alloca i32" not in final

    # stderr carries the instrumentation on LLVM 22
    stderr = result.stderr_path.read_text()
    assert "IR Dump After PromotePass on main" in stderr  # -print-changed=quiet
    assert "Running pass" in stderr  # -debug-pass-manager
    assert "Running analysis" in stderr
    assert "Total Execution Time" in stderr  # -time-passes


def test_run_opt_mtriple(toolchain, sample_ir, tmp_path):
    result = run_opt(
        sample_ir, "mem2reg", out_dir=tmp_path, mtriple="aarch64-linux-gnu",
        toolchain=toolchain,
    )
    assert not result.failed
    assert "aarch64" in result.ir_path.read_text()


def test_run_opt_unknown_pass_fails(toolchain, sample_ir, tmp_path):
    result = run_opt(sample_ir, "no-such-pass", out_dir=tmp_path, toolchain=toolchain)
    assert result.failed
    assert result.returncode != 0
    assert not result.timed_out
    assert result.ir_path is None  # no final IR on failure
    assert "unknown pass name" in result.stderr_path.read_text()


def test_run_opt_timeout(toolchain, sample_ir, tmp_path):
    result = run_opt(sample_ir, "mem2reg", out_dir=tmp_path, timeout=1e-6, toolchain=toolchain)
    assert result.timed_out
    assert result.failed


def test_run_opt_missing_input(tmp_path):
    with pytest.raises(OptError, match="input IR not found"):
        run_opt(tmp_path / "nope.ll", "mem2reg", out_dir=tmp_path)
