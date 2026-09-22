"""llc invocation (backend lane): MIR snapshots, asm emission."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from .proc import ProcError, run_capture
from .toolchain import Toolchain, discover_toolchain

DEFAULT_TIMEOUT = None  # no wall clock unless --timeout asks for one

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
    out: Path,
    load_pass_plugins: tuple[str, ...] = (),
    load: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    print_before: tuple[str, ...] = (),
    print_all: bool = True,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Build the llc invocation for the Lane B pipeline.

    *print_all* is the report's mode: every pass dumps, plus the structure and
    timing the card builders read. A caller asking about one pass turns it off.
    """
    cmd = [str(llc)]
    # New-PM plugin passes (pre-codegen IR passes) and legacy machine passes.
    cmd.extend(f"-load-pass-plugin={plugin}" for plugin in load_pass_plugins)
    cmd.extend(f"-load={plugin}" for plugin in load)
    if print_after:
        cmd.append(f"-print-after={','.join(print_after)}")
    if print_before:
        cmd.append(f"-print-before={','.join(print_before)}")
    if print_all:
        cmd.extend(["-print-after-all", "-debug-pass=Structure", "-time-passes"])
    cmd.extend(extra_args)
    cmd.extend(["-o", str(out), str(input_ir)])
    return cmd


def run_llc(
    input_ir: str | Path,
    out_dir: str | Path | None = None,
    load_pass_plugins: tuple[str, ...] = (),
    load: tuple[str, ...] = (),
    print_after: tuple[str, ...] = (),
    print_before: tuple[str, ...] = (),
    print_all: bool = True,
    extra_args: tuple[str, ...] = (),
    timeout: float | None = DEFAULT_TIMEOUT,
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
        out=asm_path,
        load_pass_plugins=load_pass_plugins, load=load,
        print_after=print_after, print_before=print_before, print_all=print_all,
        extra_args=extra_args,
    )
    asm_path.unlink(missing_ok=True)
    try:
        result = run_capture(cmd, timeout)
    except ProcError as exc:
        raise LlcError(str(exc)) from exc

    stdout_path.write_text(result.stdout)
    stderr_path.write_text(result.stderr)
    # A killed llc leaves an empty stub; a failed run reports no assembly.
    failed = result.timed_out or result.returncode != 0
    wrote = asm_path.is_file() and asm_path.stat().st_size > 0
    emitted = asm_path if (wrote and not failed) else None

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
