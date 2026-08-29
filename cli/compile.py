"""Compile a source file to LLVM IR (clang -S -emit-llvm).

Accepts:
  * C/C++ sources (.c/.cc/.cpp/.cxx/.C) -- compiled with clang at -O0 with
    the optnone attribute suppressed (-Xclang -disable-O0-optnone) so later
    optimization passes can run, plus -g for debug locations;
  * textual IR (.ll) -- passed through unchanged;
  * bitcode (.bc) -- disassembled with llvm-dis.

Crash/timeout safety: subprocesses run with a timeout, in their own process
group, and are killed as a group on timeout so no children linger; failures
raise CompileError carrying the captured stderr tail.

The result is a CompiledSource describing the IR file plus the toolchain and
command that produced it -- the pieces the report metadata needs.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import shutil
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain, ToolchainError, discover_toolchain

DEFAULT_TIMEOUT = 30  # seconds

SOURCE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".C"}

# Tokens that must appear in a plausible textual IR module (cheap sniff;
# llvm-as round-trips are the opt runner's job).
_IR_SNIFF_TOKENS = ("ModuleID", "define ", "declare ", "target triple")

_STDERR_TAIL_LINES = 20


class CompileError(RuntimeError):
    """Compilation failed, timed out, or produced unparseable IR."""


@dataclass(frozen=True)
class CompiledSource:
    source_path: Path
    ir_path: Path
    kind: str  # "clang" | "passthrough" | "llvm-dis"
    cmd: tuple[str, ...]  # command that produced ir_path
    toolchain: Toolchain
    compiled_at: str  # ISO-8601 UTC timestamp


def clang_ir_command(clang: Path, source: Path, out: Path, extra_args=()) -> list[str]:
    """Build the clang invocation that turns a C/C++ source into IR."""
    return [
        str(clang), "-S", "-emit-llvm", "-O0",
        "-Xclang", "-disable-O0-optnone",
        "-g",
        *extra_args,
        "-o", str(out), str(source),
    ]


def llvm_dis_command(llvm_dis: Path, source: Path, out: Path) -> list[str]:
    """Build the llvm-dis invocation that turns bitcode into textual IR."""
    return [str(llvm_dis), "-o", str(out), str(source)]


def _run(cmd: list[str], timeout: float) -> None:
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
    *,
    out_dir: str | Path | None = None,
    bin_dir: str | Path | None = None,
    expected_major: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_args: tuple[str, ...] = (),
    toolchain: Toolchain | None = None,
) -> CompiledSource:
    """Compile/convert *source* to textual IR and return the result.

    *toolchain* can be passed in (so the opt runner reuses the same discovery);
    otherwise it is resolved here from *bin_dir* / environment.
    """
    source = Path(source)
    if not source.is_file():
        raise CompileError(f"input not found: {source}")
    if toolchain is None:
        toolchain = discover_toolchain(bin_dir, expected_major)

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
        cmd = tuple(clang_ir_command(toolchain.clang.path, source, out, extra_args))
        _run(list(cmd), timeout)
        return CompiledSource(source, out, "clang", cmd, toolchain, compiled_at)

    if suffix == ".bc":
        cmd = tuple(llvm_dis_command(toolchain.llvm_dis.path, source, out))
        _run(list(cmd), timeout)
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


def _main(argv: list[str] | None = None) -> None:
    """Dev helper: python -m cli.compile. The full CLI is cli/main.py."""
    parser = argparse.ArgumentParser(
        prog="python -m cli.compile",
        description="Compile a source file to LLVM IR.",
    )
    parser.add_argument("source")
    parser.add_argument("-o", "--out-dir", default=None)
    parser.add_argument("--bin-dir", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    try:
        result = compile_to_ir(
            args.source, out_dir=args.out_dir,
            bin_dir=args.bin_dir, timeout=args.timeout,
        )
    except (CompileError, ToolchainError) as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"kind:   {result.kind}")
    print(f"ir:     {result.ir_path}")
    print(f"cmd:    {' '.join(result.cmd)}")
    print(f"llvm:   {result.toolchain.clang.version}")
    print(f"at:     {result.compiled_at}")


if __name__ == "__main__":
    _main()
