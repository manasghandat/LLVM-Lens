"""The example sources bundled in the package, behind `--sample`."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

# What a bare `--sample` builds.  The rest are named on the command line.
DEFAULT = "side-channel"


def directory() -> Path:
    """The packaged samples, from a source checkout or an installed wheel."""
    return Path(files("llvm_lens") / "samples")


def available() -> tuple[str, ...]:
    """The sample names, alphabetical.  A file's stem is its name."""
    try:
        names = sorted(p.stem for p in directory().iterdir() if p.suffix == ".c")
    except OSError:
        # A broken install must not take `--help` down with it; `path` reports.
        return (DEFAULT,)
    return tuple(names) or (DEFAULT,)


def path(name: str) -> Path:
    """The bundled source for *name*.  The caller checks that it exists."""
    return directory() / f"{name}.c"
