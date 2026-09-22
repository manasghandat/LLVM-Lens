"""LLVM-Lens: analyze LLVM opt/llc pass pipelines into a static report."""

from .api import PassSnapshot, SnapshotError, SnapshotRun, list_machine_passes, snapshot

__version__ = "0.1.0"

__all__ = [
    "PassSnapshot",
    "SnapshotError",
    "SnapshotRun",
    "__version__",
    "list_machine_passes",
    "snapshot",
]
