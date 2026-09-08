"""Compile a source file to LLVM IR (clang -S -emit-llvm)."""

from __future__ import annotations

import datetime as _dt
import shutil
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain

SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx"}

# Tokens that must appear in a plausible textual IR module.
_IR_SNIFF_TOKENS = ("ModuleID", "define ", "declare ", "target triple")

_STDERR_TAIL_LINES = 20


class CompileError(RuntimeError):
    """Compilation failed, timed out, or produced unparseable IR."""


@dataclass(frozen=True)
class CompiledSource:
    source_path: Path
    ir_path: Path
    kind: str  # "clang" | "passthrough" | "llvm-dis"
    cmd: list[str]  # command that produced ir_path
    toolchain: Toolchain
    compiled_at: str  # ISO-8601 UTC timestamp


def _run(cmd: list[str], timeout: float | None) -> None:
    """Run a subprocess; a non-zero exit or timeout is a CompileError."""
    try:
        result = run_capture(cmd, timeout)
    except ProcError as exc:
        raise CompileError(str(exc)) from exc
    if result.timed_out:
        raise CompileError(
            f"timed out after {timeout:g}s: {' '.join(cmd)}\n"
            f"{_tail(result.stderr)}"
        )
    if result.returncode != 0:
        raise CompileError(
            f"exit {result.returncode}: {' '.join(cmd)}\n{_tail(result.stderr)}"
        )


def _tail(text: str | None, lines: int = _STDERR_TAIL_LINES) -> str:
    if not text:
        return "(no output)"
    return "\n".join(text.splitlines()[-lines:])


def _looks_like_ir(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return False
    return any(token in head for token in _IR_SNIFF_TOKENS)


def _output_name(source: Path) -> str:
    if source.suffix == ".ll":
        return source.name
    return source.stem + ".ll"


def compile_to_ir(
    source: str | Path,
    toolchain: Toolchain,
    out_dir: str | Path | None = None,
    timeout: float | None = None,
    extra_args: tuple[str, ...] = (),
) -> CompiledSource:
    """Compile/convert *source* to textual IR and return the result."""
    source = Path(source)
    if not source.is_file():
        raise CompileError(f"input not found: {source}")

    out_dir = Path(out_dir) if out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / _output_name(source)
    if out.resolve() == source.resolve():
        raise CompileError(
            f"output {out} would overwrite the input; pick a different --out-dir"
        )

    compiled_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    suffix = source.suffix

    if suffix in SOURCE_EXTS:
        cmd = [
            str(toolchain.clang.path), "-S", "-emit-llvm", "-O0",
            "-Xclang", "-disable-O0-optnone",
            "-g",
            *extra_args,
            "-o", str(out), str(source)
        ]
        _run(cmd, timeout)
        return CompiledSource(source, out, "clang", cmd, toolchain, compiled_at)

    if suffix == ".bc":
        cmd = [str(toolchain.llvm_dis.path), "-o", str(out), str(source)]
        _run(cmd, timeout)
        return CompiledSource(source, out, "llvm-dis", cmd, toolchain, compiled_at)

    if suffix == ".ll":
        cmd = ("cp", str(source), str(out))
        shutil.copyfile(source, out)
        if not _looks_like_ir(out):
            raise CompileError(
                f"{source} does not look like textual LLVM IR "
                f"(no ModuleID/define/declare in the head of the file)"
            )
        return CompiledSource(source, out, "passthrough", cmd, toolchain, compiled_at)

    raise CompileError(
        f"unsupported input extension {suffix!r} (supported: "
        + ", ".join(sorted(SOURCE_EXTS | {".ll", ".bc"})) + ")"
    )
