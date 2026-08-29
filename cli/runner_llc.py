"""llc invocation (backend lane): MIR snapshots, asm emission.

Runs llc on the final IR with the instrumentation the report needs:
  * -print-after-all     -- per-pass IR + machine-code dumps (stderr)
  * -debug-pass=Structure-- ordered backend pass structure (stderr)
  * -time-passes         -- per-pass timing (stderr)
  * -o <file>.s          -- final assembly (llc emits asm by default; no -S)

Like runner_opt, a failing or timed-out llc is a *result*, not an exception:
the report is partial, with the crash stack trace captured in stderr.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain, discover_toolchain

DEFAULT_TIMEOUT = 60  # seconds

ASM_NAME = "final.s"
STDOUT_LOG_NAME = "llc-stdout.log"
STDERR_LOG_NAME = "llc-stderr.log"


class LlcError(RuntimeError):
    """Precondition failure: bad args or unstartable llc (not an llc crash)."""


@dataclass(frozen=True)
class LlcResult:
    input_ir: Path
    asm_path: Path | None  # final assembly; None if llc failed to emit
    stdout_path: Path
    stderr_path: Path
    cmd: tuple[str, ...]
    returncode: int
    timed_out: bool
    toolchain: Toolchain
    ran_at: str  # ISO-8601 UTC timestamp

    @property
    def failed(self) -> bool:
        return self.timed_out or self.returncode != 0


def llc_command(
    llc: Path,
    input_ir: Path,
    *,
    out: Path,
    mtriple: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Build the llc invocation for the Lane B pipeline."""
    cmd = [str(llc)]
    if mtriple:
        cmd.append(f"-mtriple={mtriple}")
    cmd.extend(["-print-after-all", "-debug-pass=Structure", "-time-passes"])
    cmd.extend(extra_args)
    cmd.extend(["-o", str(out), str(input_ir)])
    return cmd


def run_llc(
    input_ir: str | Path,
    *,
    out_dir: str | Path | None = None,
    mtriple: str | None = None,
    extra_args: tuple[str, ...] = (),
    timeout: float = DEFAULT_TIMEOUT,
    toolchain: Toolchain | None = None,
) -> LlcResult:
    """Run llc on *input_ir* and capture everything the parsers need."""
    input_ir = Path(input_ir)
    if not input_ir.is_file():
        raise LlcError(f"input IR not found: {input_ir}")
    if toolchain is None:
        toolchain = discover_toolchain()

    out_dir = Path(out_dir) if out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    asm_path = out_dir / ASM_NAME
    stdout_path = out_dir / STDOUT_LOG_NAME
    stderr_path = out_dir / STDERR_LOG_NAME

    cmd = llc_command(
        toolchain.llc.path, input_ir,
        out=asm_path, mtriple=mtriple, extra_args=extra_args,
    )
    try:
        result = run_capture(cmd, timeout)
    except ProcError as exc:
        raise LlcError(str(exc)) from exc

    stdout_path.write_text(result.stdout)
    stderr_path.write_text(result.stderr)
    emitted = asm_path if asm_path.is_file() else None

    return LlcResult(
        input_ir=input_ir,
        asm_path=emitted,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        cmd=tuple(cmd),
        returncode=result.returncode,
        timed_out=result.timed_out,
        toolchain=toolchain,
        ran_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    )


def _main(argv: list[str] | None = None) -> None:
    """Dev helper: python -m cli.runner_llc. The full CLI is cli/main.py."""
    parser = argparse.ArgumentParser(
        prog="python -m cli.runner_llc",
        description="Run llc on an IR module with instrumentation.",
    )
    parser.add_argument("input_ir")
    parser.add_argument("-o", "--out-dir", default=None)
    parser.add_argument("--mtriple", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    try:
        result = run_llc(args.input_ir, out_dir=args.out_dir, mtriple=args.mtriple, timeout=args.timeout)
    except LlcError as exc:
        raise SystemExit(f"error: {exc}") from exc

    status = "timeout" if result.timed_out else f"exit {result.returncode}"
    print(f"status:  {status}")
    print(f"asm:     {result.asm_path or '(none)'}")
    print(f"stdout:  {result.stdout_path}")
    print(f"stderr:  {result.stderr_path}")
    print(f"cmd:     {' '.join(result.cmd)}")


if __name__ == "__main__":
    _main()
