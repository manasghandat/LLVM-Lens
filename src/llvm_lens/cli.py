"""Command-line interface (argparse): one command that builds a report."""

from __future__ import annotations

import argparse
import getpass
import sys
import textwrap
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from . import __version__, sample
from .config import (
    DEFAULT_BASE_URLS, DEFAULT_MODELS, PROVIDERS, ConfigError, config_path,
    load_config, save_config,
)
from .report import build_report
from .settings import (
    DEFAULT_PASSES, SETTINGS_ENV, PROJECT_FILENAMES, Settings, SettingsError,
    load_settings, write_template,
)

DEFAULT_CONFIG_NAME = PROJECT_FILENAMES[0]

# LLVM's own flags all start with a dash, which argparse would read as the
# next option; joining them with "=" is what survives that.
_DASH_NOTE = "Write --opt-arg=-foo when the argument starts with '-'."


class _Formatter(argparse.HelpFormatter):
    """Wrap help text on spaces only: `llvm-lens.yml` and `switch-lowering`
    are one word each, and splitting them at the hyphen reads as a typo."""

    def _split_lines(self, text: str, width: int) -> list[str]:
        return textwrap.wrap(text, width, break_on_hyphens=False)

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        return textwrap.fill(text, width, initial_indent=indent,
                             subsequent_indent=indent, break_on_hyphens=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llvm-lens",
        formatter_class=_Formatter,
        description=(
            "Analyze LLVM pass pipelines and emit a static HTML report. "
            "Accepts .c/.cpp (compiled with clang), .ll, and .bc sources. "
            "Lane A runs the opt middle-end pipeline; Lane B runs the llc backend. "
            "Settings come from the nearest " + DEFAULT_CONFIG_NAME + ", then "
            f"{SETTINGS_ENV}; every flag below overrides them, and "
            "`llvm-lens --init-config` writes a starter file."
        ),
        epilog=(
            "Repeatable flags (--load-pass-plugin, --custom-pass, and the "
            "--*-arg flags) add to whatever the config file already lists, "
            "rather than replacing it."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "source", nargs="?", help="Source file to analyze (.c/.cpp/.ll/.bc).",
    )
    parser.add_argument(
        "--config", metavar="FILE", default=None,
        help=f"Settings file to use, instead of searching for {DEFAULT_CONFIG_NAME}. "
             "See `--init-config` for the keys.",
    )
    parser.add_argument(
        "--no-config", dest="no_config", action="store_true",
        help="Ignore every settings file and use the built-in defaults.",
    )
    parser.add_argument(
        "--init-config", nargs="?", const=DEFAULT_CONFIG_NAME, default=None,
        metavar="FILE",
        help=f"Write a commented starter settings file (default: "
             f"{DEFAULT_CONFIG_NAME}) and exit. Never overwrites.",
    )
    parser.add_argument(
        "--sample", nargs="?", const=sample.DEFAULT, default=None,
        choices=sample.available(), metavar="NAME",
        help=f"Build a report for one of the bundled example sources "
             f"({', '.join(sample.available())}; default: {sample.DEFAULT}) "
             "instead of naming a file.  Reads no settings file.",
    )
    parser.add_argument(
        "--passes", default=None,
        help=f"New-PM pipeline string for opt (Lane A).  [default: {DEFAULT_PASSES}]",
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
        "-o", "--output", default=None,
        help="Directory to write the report into.  [default: report]",
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
        "--target", default=None, metavar="TRIPLE",
        help="Target triple, passed to clang as --target and to llc as "
             "-mtriple.  Defaults to the host.",
    )
    parser.add_argument(
        "--timeout", type=float, default=None, metavar="SECONDS",
        help="Per-tool invocation timeout. Off by default: a big module under "
             "default<O2> is slow rather than hung.",
    )
    parser.add_argument(
        "--source-map", action=argparse.BooleanOptionalAction, default=None,
        help="Correlate IR/MIR lines with the original source (needs debug "
             "info; costs one extra llc run).",
    )
    parser.add_argument(
        "--open", dest="open_report", action="store_true", default=None,
        help="Open the report in the default browser once it is written.",
    )
    parser.add_argument(
        "--ai", action=argparse.BooleanOptionalAction, default=None,
        help="Copy the stored AI credentials (see configure-ai) into the "
             "report.  Use --no-ai before sharing a report directory.",
    )
    parser.add_argument(
        "--clang-arg", dest="clang_args", action="append", default=[], metavar="ARG",
        help=f"Extra argument for the clang invocation (repeatable). {_DASH_NOTE}",
    )
    parser.add_argument(
        "--opt-arg", dest="opt_args", action="append", default=[], metavar="ARG",
        help=f"Extra argument for the opt invocation (repeatable). {_DASH_NOTE}",
    )
    parser.add_argument(
        "--llc-arg", dest="llc_args", action="append", default=[], metavar="ARG",
        help=f"Extra argument for the llc invocation (repeatable). {_DASH_NOTE}",
    )
    return parser


def read_settings(args: argparse.Namespace) -> Settings:
    """The config file's settings. Raise SystemExit on a file we cannot use."""
    # A sample is a demo of the built-in defaults: a stray llvm-lens.yml three
    # directories up must not be able to break it.
    if args.no_config or args.sample is not None:
        return Settings()
    try:
        return load_settings(args.config)
    except SettingsError as exc:
        raise SystemExit(f"error: {exc}") from exc


def apply_flags(settings: Settings, args: argparse.Namespace) -> Settings:
    """Flags win over the file; the repeatable ones add to it."""
    return settings.overridden(
        passes=args.passes,
        output=args.output,
        bin_dir=args.bin_dir,
        llvm_version=args.llvm_version,
        target=args.target,
        timeout=args.timeout,
        source_map=args.source_map,
        open_report=args.open_report,
        ai=args.ai,
        custom_passes=settings.custom_passes + tuple(args.custom_passes),
        pass_plugins=settings.pass_plugins + tuple(args.load_pass_plugins),
        legacy_plugins=settings.legacy_plugins + tuple(args.load),
        clang_args=settings.clang_args + tuple(args.clang_args),
        opt_args=settings.opt_args + tuple(args.opt_args),
        llc_args=settings.llc_args + tuple(args.llc_args),
    )


def build_configure_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llvm-lens configure-ai",
        formatter_class=_Formatter,
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


def run_init_config(path: str) -> int:
    try:
        written = write_template(path)
    except SettingsError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(f"wrote {written}")
    print("every key is optional and shown at its default; flags still win over it")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    if words and words[0] in ("configure-ai", "--configure-ai"):
        return run_configure_ai(words[1:])

    parser = build_parser()
    args = parser.parse_args(words)

    if args.init_config is not None:
        return run_init_config(args.init_config)
    if args.config and args.no_config:
        parser.error("--config and --no-config are mutually exclusive")
    if args.sample is not None:
        if args.source is not None:
            parser.error("source and --sample are mutually exclusive")
        if args.config:
            # Silently dropping a file the user named is worse than saying so.
            parser.error("--config and --sample are mutually exclusive")
        source = sample.path(args.sample)
        if not source.is_file():
            parser.error(
                f"argument --sample: {args.sample!r} is missing from this install "
                f"(looked in {sample.directory()})"
            )
    elif args.source is None:
        parser.error("the following arguments are required: source")
    else:
        source = Path(args.source)
        if not source.is_file():
            parser.error(f"argument source: {args.source!r} is not an existing file")

    settings = apply_flags(read_settings(args), args)
    if settings.config_file:
        print(f"config:     {settings.config_file}")
    if args.sample is not None:
        print(f"sample:     {args.sample}")

    try:
        summary = build_report(
            source,
            passes=settings.passes,
            load_pass_plugins=settings.pass_plugins,
            load=settings.legacy_plugins,
            custom_passes=settings.custom_passes,
            output=settings.output,
            bin_dir=settings.bin_dir,
            llvm_version=settings.llvm_version,
            target=settings.target,
            timeout=settings.timeout,
            source_map=settings.source_map,
            ai_config=load_config() if settings.ai else {},
            clang_args=settings.clang_args,
            opt_args=settings.opt_args,
            llc_args=settings.llc_args,
            ui=settings.ui,
            config_file=settings.config_file,
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
    if settings.open_report:
        webbrowser.open((Path(summary["reportDir"]) / "index.html").as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
