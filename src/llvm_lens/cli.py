"""Command-line interface (argparse): one command that builds a report."""

from __future__ import annotations

import argparse
import getpass
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import (
    DEFAULT_BASE_URLS, DEFAULT_MODELS, PROVIDERS, ConfigError, config_path,
    load_config, save_config,
)
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
    parser.add_argument(
        "--open", dest="open_report", action="store_true",
        help="Open the report in the default browser once it is written.",
    )
    parser.add_argument(
        "--no-ai", dest="ai", action="store_false", default=True,
        help="Do not copy the stored AI credentials (see configure-ai) into "
             "the report.  Use this before sharing a report directory.",
    )
    return parser


def build_configure_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llvm-lens configure-ai",
        description=(
            "Store the AI provider and API key used by the report's \"ask AI\" "
            f"panel. Written to {config_path()} (0600) and copied into each "
            "report's gitignored data/ai-config.* sidecar -- never into the "
            "report's HTML or manifest. Run once; every report picks it up."
        ),
    )
    parser.add_argument(
        "--provider", choices=PROVIDERS, default=None,
        help="Which API to speak.  [prompted if omitted]",
    )
    parser.add_argument(
        "--api-key", default=None, metavar="KEY",
        help="API key. Omit to be prompted (input is not echoed).",
    )
    parser.add_argument(
        "--model", default=None,
        help=f"Model id.  [default: {DEFAULT_MODELS['anthropic']} for anthropic, "
             "prompted otherwise]",
    )
    parser.add_argument(
        "--base-url", default=None,
        help="Endpoint root. Only meaningful for openai-compatible "
             f"(OpenRouter, Ollama, ...).  [default: {DEFAULT_BASE_URLS['openai-compatible']}]",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print the current configuration (key masked) and exit.",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Delete the stored configuration and exit.",
    )
    return parser


def _mask(key: str | None) -> str:
    """Never print a key back: show its last four characters at most."""
    if not key:
        return "(unset)"
    return "•" * 8 + key[-4:] if len(key) > 4 else "•" * len(key)


def _ask(prompt: str, current: str | None = None, secret: bool = False) -> str:
    """Prompt for a value; an empty answer keeps *current*."""
    suffix = " [keep existing]" if current else ""
    reader = getpass.getpass if secret else input
    try:
        answer = reader(f"{prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (current or "")


def run_configure_ai(argv: Sequence[str]) -> int:
    parser = build_configure_parser()
    args = parser.parse_args(argv)
    path = config_path()

    if args.clear:
        try:
            path.unlink()
            print(f"removed {path}")
        except FileNotFoundError:
            print(f"no configuration at {path}")
        # The file is not the only copy. Say so, or `--clear` looks like it did
        # nothing to anyone whose panel is still answering: report directories
        # built earlier carry their own copy, and the panel can hold a key that
        # was typed into it, which no command-line tool can reach.
        print("reports built before now keep their own copy in "
              "data/ai-config.* -- rebuild them with --no-ai to drop it.\n"
              "a key set in the report panel itself lives in that browser, not "
              "here: open the panel's ⚙ and use forget.")
        return 0

    existing = load_config() or {}
    if args.show:
        if not existing:
            print(f"no configuration at {path}")
            return 1
        for field in ("provider", "model", "base_url"):
            if existing.get(field):
                print(f"{field + ':':10} {existing[field]}")
        print(f"{'api_key:':10} {_mask(existing.get('api_key'))}")
        print(f"{'config:':10} {path}")
        return 0

    interactive = sys.stdin.isatty()
    provider = args.provider or existing.get("provider")
    if provider is None:
        if not interactive:
            parser.error("--provider is required when stdin is not a terminal")
        provider = _ask(f"provider ({', '.join(PROVIDERS)})", "anthropic")
    if provider not in PROVIDERS:
        parser.error(f"unknown provider {provider!r}")

    # A blank --api-key falls through to the prompt, so a flag-less run can
    # still pick up an existing key without retyping it.
    api_key = args.api_key
    if api_key is None and interactive:
        api_key = _ask("api key", existing.get("api_key"), secret=True)
    if api_key is None:
        api_key = existing.get("api_key", "")

    model = args.model
    if model is None:
        default_model = existing.get("model") or DEFAULT_MODELS[provider]
        model = default_model if not interactive else _ask("model", default_model)

    base_url = args.base_url
    if base_url is None:
        default_url = existing.get("base_url") or DEFAULT_BASE_URLS[provider]
        # Only worth asking for the configurable protocol; anthropic's host is
        # fixed and a prompt there is noise.
        if provider == "openai-compatible" and interactive:
            base_url = _ask("base url", default_url)
        else:
            base_url = default_url

    try:
        written = save_config({
            "provider": provider,
            "api_key": api_key,
            "model": model,
            "base_url": base_url,
        })
    except ConfigError as exc:
        parser.error(str(exc))

    print(f"provider: {provider}")
    if model:
        print(f"model:    {model}")
    if base_url:
        print(f"base url: {base_url}")
    print(f"api key:  {_mask(api_key)}")
    print(f"written:  {written}")
    if not api_key:
        print("warning: no api key stored; the report's ask-AI panel stays off",
              file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # configure-ai is not a report build, and the build parser wants a source
    # file it would reject as missing. Dispatch it before argparse sees argv.
    words = list(sys.argv[1:] if argv is None else argv)
    if words and words[0] in ("configure-ai", "--configure-ai"):
        return run_configure_ai(words[1:])

    parser = build_parser()
    args = parser.parse_args(words)

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
            ai_config=load_config() if args.ai else {},
        )
    except Exception as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"report:     {summary['reportDir']}/")
    print(f"manifest:   {summary['manifest']}")
    if summary.get("aiEmbedded"):
        print("note:       AI credentials copied into the report "
              "(data/ai-config.*); rebuild with --no-ai before sharing it")
    print(f"passes:     {summary['laneACount']} IR, {summary['laneBCount']} machine")
    print(f"total time: {summary['totalTimeMs']:g} ms")
    if summary["optCrashed"]:
        print("warning: opt failed/timed out; report is partial", file=sys.stderr)
    if summary["llcCrashed"]:
        print("warning: llc failed/timed out; report is partial", file=sys.stderr)
    if args.open_report:
        webbrowser.open((Path(summary["reportDir"]) / "index.html").as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
