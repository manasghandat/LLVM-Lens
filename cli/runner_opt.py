"""opt invocation (new pass manager): pipeline runs, plugins, capture.

Runs opt on an IR module with the instrumentation the report needs:
  * -print-changed=quiet  -- per-pass before/after IR dumps of changed functions
  * -debug-pass-manager   -- analyses run/cached/invalidated (new PM)
  * -time-passes          -- per-pass timing

Unlike compile.py, a failing or timed-out opt is a *result*, not an
exception: the report spec wants a partial report (with the crash stack trace)
when a pass crashes. The result carries the captured stdout/stderr files and
the final IR path (absent if opt failed before emitting).

Note: on LLVM 22, -print-changed dumps go to stderr (errs()), not stdout.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain, discover_toolchain

DEFAULT_TIMEOUT = None  # no wall clock unless --timeout asks for one

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
    load_pass_plugins: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Build the opt invocation for the Lane A pipeline.

    Shared with the source-map harvest, which re-runs the same pipeline with
    -print-module-scope: the two command lines have to agree flag for flag or
    the harvested dump sequence stops lining up with the main run's.
    """
    cmd = [str(opt), "-S", f"-passes={passes}"]
    cmd.extend(f"-load-pass-plugin={plugin}" for plugin in load_pass_plugins)
    cmd.extend(["-print-changed=quiet", "-debug-pass-manager", "-time-passes"])
    # -print-after takes a comma-separated list of pass names: force a dump
    # for named custom passes even when they do not change IR (analysis passes).
    if print_after:
        cmd.append(f"-print-after={','.join(print_after)}")
    cmd.extend(extra_args)
    cmd.extend(["-o", str(out), str(input_ir)])
    return cmd


def run_opt(
    toolchain: Toolchain,
    input_ir: str | Path,
    passes: str,
    out_dir: str | Path | None = None,
    load_pass_plugins: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    extra_args: tuple[str, ...] = (),
    timeout: float | None = DEFAULT_TIMEOUT,
) -> OptResult:
    """Run *passes* on *input_ir* and capture everything the parsers need."""
    input_ir = Path(input_ir)
    if not input_ir.is_file():
        raise OptError(f"input IR not found: {input_ir}")

    out_dir = Path(out_dir) if out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    final_ir = out_dir / FINAL_IR_NAME
    stdout_path = out_dir / CHANGED_LOG_NAME
    stderr_path = out_dir / STDERR_LOG_NAME

    cmd = opt_command(
        toolchain.opt.path, input_ir, passes,
        out=final_ir,
        load_pass_plugins=load_pass_plugins, print_after=print_after,
        extra_args=extra_args,
    )
    # A report is rebuilt over its own directory, so an earlier build's
    # opt-final.ll may already be sitting here. Drop it first, so the file
    # existing afterwards means *this* run emitted it.
    final_ir.unlink(missing_ok=True)
    try:
        result = run_capture(cmd, timeout)
    except ProcError as exc:
        raise OptError(str(exc)) from exc

    stdout_path.write_text(result.stdout)
    stderr_path.write_text(result.stderr)
    # opt opens -o at startup, so a killed opt leaves the file truncated to
    # nothing, while a clean non-zero exit makes LLVM delete it itself. A
    # 0-byte husk is not a module lane B can run on -- llc reads it happily
    # and emits an empty backend lane that reads as healthy -- so a failed run
    # reports no final IR rather than a path to one.
    failed = result.timed_out or result.returncode != 0
    emitted = final_ir.is_file() and final_ir.stat().st_size > 0
    ir_path = final_ir if (emitted and not failed) else None

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
