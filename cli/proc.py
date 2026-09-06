"""Subprocess helpers shared by the runners: capture, timeout, group kill.

Every invocation runs in its own process group (start_new_session=True) so a
timeout can kill the whole tree with killpg -- no orphaned clang/opt children.

A *timeout* of None means wait indefinitely, which is the default the CLI
passes: a long pipeline on a big module is slow, not hung, and a wall clock
that truncates it produces a partial report that reads like a compiler bug.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass


class ProcError(RuntimeError):
    """The subprocess could not be started at all."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def run_capture(cmd: list[str], timeout: float | None = None) -> RunResult:
    """Run *cmd*, capture stdout/stderr, kill the process group on timeout.
    *timeout* of None waits indefinitely, so ``timed_out`` is never True.
    Never raises for a non-zero exit or a timeout -- callers decide how to
    treat those (compile.py raises, runner_opt.py records them as results).
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
    except OSError as exc:
        raise ProcError(f"cannot start {cmd[0]}: {exc}") from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return RunResult(proc.returncode, stdout, stderr, timed_out=False)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Reap and drain remaining output so nothing lingers.
        stdout, stderr = proc.communicate()
        return RunResult(proc.returncode, stdout, stderr, timed_out=True)
