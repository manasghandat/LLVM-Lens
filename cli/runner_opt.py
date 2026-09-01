"""opt invocation (new pass manager): pipeline runs, plugins, capture.

Runs opt on an IR module with the instrumentation the report needs:
  * -print-changed=quiet  -- per-pass before/after IR dumps of changed functions
  * -debug-pass-manager   -- analyses run/cached/invalidated (new PM)
  * -time-passes          -- per-pass timing
  * -mtriple / -load-pass-plugin / extra_args pass through unchanged

Unlike compile.py, a failing or timed-out opt is a *result*, not an
exception: the report spec wants a partial report (with the crash stack trace)
when a pass crashes. The result carries the captured stdout/stderr files and
the final IR path (absent if opt failed before emitting).

Note: on LLVM 22, -print-changed dumps go to stderr (errs()), not stdout.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain, discover_toolchain

DEFAULT_TIMEOUT = 60  # seconds; opt pipelines can be slow

FINAL_IR_NAME = "opt-final.ll"
CHANGED_LOG_NAME = "opt-changed.log"  # stdout (usually empty on LLVM 22)
STDERR_LOG_NAME = "opt-stderr.log"  # IR dumps, analyses, timings, LLVM_DEBUG


class OptError(RuntimeError):
    """Precondition failure: bad args or unstartable opt (not an opt crash)."""


@dataclass(frozen=True)
class OptResult:
    input_ir: Path
    ir_path: Path | None  # final IR written via -o; None if opt failed to emit
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


def opt_command(
    opt: Path,
    input_ir: Path,
    passes: str,
    *,
    out: Path,
    mtriple: str | None = None,
    load_pass_plugins: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Build the opt invocation for the Lane A pipeline."""
    cmd = [str(opt), "-S"]
    if mtriple:
        cmd.append(f"-mtriple={mtriple}")
    cmd.append(f"-passes={passes}")
    cmd.extend(f"-load-pass-plugin={plugin}" for plugin in load_pass_plugins)
    cmd.extend(
        ["-print-changed=quiet", "-debug-pass-manager", "-time-passes"]
    )
    # -print-after takes a comma-separated list of pass names: force a dump
    # for named custom passes even when they do not change IR (analysis passes).
    if print_after:
        cmd.append(f"-print-after={','.join(print_after)}")
    cmd.extend(extra_args)
    cmd.extend(["-o", str(out), str(input_ir)])
    return cmd


def run_opt(
    input_ir: str | Path,
    passes: str,
    *,
    out_dir: str | Path | None = None,
    mtriple: str | None = None,
    load_pass_plugins: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
    timeout: float = DEFAULT_TIMEOUT,
    toolchain: Toolchain | None = None,
) -> OptResult:
    """Run *passes* on *input_ir* and capture everything the parsers need."""
    input_ir = Path(input_ir)
    if not input_ir.is_file():
        raise OptError(f"input IR not found: {input_ir}")
    if toolchain is None:
        toolchain = discover_toolchain()

    out_dir = Path(out_dir) if out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    final_ir = out_dir / FINAL_IR_NAME
    stdout_path = out_dir / CHANGED_LOG_NAME
    stderr_path = out_dir / STDERR_LOG_NAME

    cmd = opt_command(
        toolchain.opt.path, input_ir, passes,
        out=final_ir, mtriple=mtriple,
        load_pass_plugins=load_pass_plugins, print_after=print_after,
        extra_args=extra_args,
    )
    try:
        result = run_capture(cmd, timeout)
    except ProcError as exc:
        raise OptError(str(exc)) from exc

    stdout_path.write_text(result.stdout)
    stderr_path.write_text(result.stderr)
    ir_path = final_ir if final_ir.is_file() else None

    return OptResult(
        input_ir=input_ir,
        ir_path=ir_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        cmd=tuple(cmd),
        returncode=result.returncode,
        timed_out=result.timed_out,
        toolchain=toolchain,
        ran_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
    )


def _main(argv: list[str] | None = None) -> None:
    """Dev helper: python -m cli.runner_opt. The full CLI is cli/main.py."""
    parser = argparse.ArgumentParser(
        prog="python -m cli.runner_opt",
        description="Run an opt pipeline on an IR module with instrumentation.",
    )
    parser.add_argument("input_ir")
    parser.add_argument("--passes", required=True)
    parser.add_argument("-o", "--out-dir", default=None)
    parser.add_argument("--mtriple", default=None)
    parser.add_argument("--load-pass-plugin", action="append", default=[])
    parser.add_argument("--print-after", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    try:
        result = run_opt(
            args.input_ir, args.passes, out_dir=args.out_dir,
            mtriple=args.mtriple,
            load_pass_plugins=tuple(args.load_pass_plugin),
            print_after=tuple(args.print_after),
            timeout=args.timeout,
        )
    except OptError as exc:
        raise SystemExit(f"error: {exc}") from exc

    status = "timeout" if result.timed_out else f"exit {result.returncode}"
    print(f"status:  {status}")
    print(f"final:   {result.ir_path or '(none)'}")
    print(f"stdout:  {result.stdout_path}")
    print(f"stderr:  {result.stderr_path}")
    print(f"cmd:     {' '.join(result.cmd)}")


if __name__ == "__main__":
    _main()
