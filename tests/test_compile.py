"""Tests for llvm_lens.compile (and the toolchain discovery it builds on)."""

from __future__ import annotations

from pathlib import Path

import pytest

from llvm_lens.compile import CompileError, compile_to_ir
from tests.conftest import SAMPLE_C


# --- end-to-end against a real toolchain (skips when unavailable) -----------


def test_compile_c_to_ir(toolchain, tmp_path):
    result = compile_to_ir(SAMPLE_C, out_dir=tmp_path, toolchain=toolchain,
                           extra_args=("-DNDEBUG",))
    assert result.kind == "clang"
    # The output name follows the source's stem, whatever the sample is called.
    assert result.ir_path == tmp_path / f"{SAMPLE_C.stem}.ll"
    assert list(result.cmd) == [
        str(toolchain.clang.path), "-S", "-emit-llvm", "-O0",
        "-Xclang", "-disable-O0-optnone",
        "-g", "-DNDEBUG",
        "-o", str(tmp_path / f"{SAMPLE_C.stem}.ll"), str(SAMPLE_C),
    ]
    assert "clang" in result.toolchain.clang.version
    text = result.ir_path.read_text()
    assert "define" in text
    assert "optnone" not in text  # -Xclang -disable-O0-optnone must hold
    assert "!dbg" in text  # -g debug locations must be present


def test_compile_ll_passthrough(toolchain, tmp_path):
    src = tmp_path / "mod.ll"
    src.write_text("; test\nModuleID = 'x'\ndefine void @f() {\n  ret void\n}\n")
    result = compile_to_ir(src, out_dir=tmp_path / "out", toolchain=toolchain)
    assert result.kind == "passthrough"
    assert result.ir_path == tmp_path / "out" / "mod.ll"
    assert "define void @f" in result.ir_path.read_text()


def test_compile_ll_refuses_to_overwrite_input(toolchain, tmp_path):
    src = tmp_path / "mod.ll"
    src.write_text("; test\ndefine void @f() {\n  ret void\n}\n")
    with pytest.raises(CompileError, match="would overwrite the input"):
        compile_to_ir(src, out_dir=tmp_path, toolchain=toolchain)


def test_compile_ll_rejects_garbage(toolchain, tmp_path):
    src = tmp_path / "junk.ll"
    src.write_text("this is definitely not LLVM IR\n")
    with pytest.raises(CompileError, match="does not look like textual LLVM IR"):
        compile_to_ir(src, out_dir=tmp_path / "out", toolchain=toolchain)


def test_compile_bc_via_llvm_dis(toolchain, tmp_path):
    import subprocess

    bc = tmp_path / "sample.bc"
    subprocess.run(
        [str(toolchain.clang.path), "-c", "-emit-llvm", "-O0", str(SAMPLE_C), "-o", str(bc)],
        check=True, capture_output=True, text=True,
    )
    result = compile_to_ir(bc, out_dir=tmp_path, toolchain=toolchain)
    assert result.kind == "llvm-dis"
    text = result.ir_path.read_text()
    assert "define" in text


def test_compile_unsupported_extension(toolchain, tmp_path):
    src = tmp_path / "x.rs"
    src.write_text("fn main() {}\n")
    with pytest.raises(CompileError, match="unsupported input extension"):
        compile_to_ir(src, out_dir=tmp_path, toolchain=toolchain)


def test_compile_missing_source(toolchain, tmp_path):
    with pytest.raises(CompileError, match="input not found"):
        compile_to_ir(tmp_path / "nope.c", out_dir=tmp_path, toolchain=toolchain)


def test_toolchain_major_sanity(toolchain):
    assert toolchain.expected_major == 22
    for tool in toolchain.tools.values():
        assert tool.major == 22, tool
