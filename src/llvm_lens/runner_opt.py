"""opt invocation (new pass manager): pipeline runs, plugins, capture."""

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

    cmd = [
        str(toolchain.opt.path), "-S",
        f"-passes={passes}",
        "-print-changed=quiet", "-print-module-scope", "-debug-pass-manager",
        "-time-passes",
    ]
    cmd.extend(f"-load-pass-plugin={plugin}" for plugin in load_pass_plugins)
    # -print-after takes a comma-separated list; force a dump for custom passes.
    if print_after:
        cmd.append(f"-print-after={','.join(print_after)}")
    cmd.extend([*extra_args, "-o", str(final_ir), str(input_ir)])
    # Drop a previous build's output so an existing file means *this* run wrote it.
    final_ir.unlink(missing_ok=True)
    try:
        result = run_capture(cmd, timeout)
    except ProcError as exc:
        raise OptError(str(exc)) from exc

    stdout_path.write_text(result.stdout)
    stderr_path.write_text(result.stderr)
    # A failed opt leaves no usable final IR; report none.
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
