"""Command-line interface (argparse): one command that builds a report."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .report import DEFAULT_PASSES, build_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llvm-lens",
        description=(
            "Analyze LLVM pass pipelines and emit a static HTML report. "
            "Accepts .c/.cpp (compiled with clang), .ll, and .bc sources. "
            "Lane A runs the opt middle-end pipeline; Lane B runs the llc backend."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument("source", help="Source file to analyze (.c/.cpp/.ll/.bc).")
    parser.add_argument(
        "--passes", default=DEFAULT_PASSES,
        help="New-PM pipeline string for opt (Lane A).  [default: %(default)s]",
    )
    parser.add_argument(
        "--load-pass-plugin", dest="load_pass_plugins", action="append",
        default=[], metavar="SO",
        help="New-PM pass plugin .so to load (opt + llc, repeatable).",
    )
    parser.add_argument(
        "--load", dest="load", action="append", default=[], metavar="SO",
        help="Legacy plugin .so to load (llc backend only, repeatable).",
    )
    parser.add_argument(
        "--custom-pass", dest="custom_passes", action="append",
        default=[], metavar="NAME",
        help="Custom function-pass name to append (as function(<name>)) "
             "and badge (repeatable).",
    )
    parser.add_argument(
        "-o", "--output", default="report",
        help="Directory to write the report into.  [default: %(default)s]",
    )
    parser.add_argument(
        "--bin-dir", default=None,
        help="Directory holding the LLVM tools (else LLVM_LENS_BIN_DIR / PATH).",
    )
    parser.add_argument(
        "--llvm-version", type=int, default=None, metavar="MAJOR",
        help="Expected LLVM major version (drives the PATH search). "
             "Defaults to LLVM_LENS_LLVM_MAJOR, else 22.",
    )
    parser.add_argument(
        "--timeout", type=float, default=None, metavar="SECONDS",
        help="Per-tool invocation timeout. Off by default: a big module under "
             "default<O2> is slow rather than hung.",
    )
    parser.add_argument(
        "--no-source-map", dest="source_map", action="store_false", default=True,
        help="Correlate IR/MIR lines with the original source (needs debug "
             "info; costs one extra llc run).  [default: on]",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_file():
        parser.error(f"argument source: {args.source!r} is not an existing file")

    try:
        summary = build_report(
            source,
            passes=args.passes,
            load_pass_plugins=tuple(args.load_pass_plugins),
            load=tuple(args.load),
            custom_passes=tuple(args.custom_passes),
            output=args.output,
            bin_dir=args.bin_dir,
            llvm_version=args.llvm_version,
            timeout=args.timeout,
            source_map=args.source_map,
        )
    except Exception as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"report:     {summary['reportDir']}/")
    print(f"manifest:   {summary['manifest']}")
    print(f"passes:     {summary['laneACount']} IR, {summary['laneBCount']} machine")
    print(f"total time: {summary['totalTimeMs']:g} ms")
    if summary["optCrashed"]:
        print("warning: opt failed/timed out; report is partial", file=sys.stderr)
    if summary["llcCrashed"]:
        print("warning: llc failed/timed out; report is partial", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
