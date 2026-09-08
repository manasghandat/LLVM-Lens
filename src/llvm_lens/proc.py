"""Subprocess helpers: capture, timeout, group kill."""

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
    """Run *cmd*, capture stdout/stderr, kill the process group on timeout."""
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
