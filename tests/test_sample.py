"""`--sample`: the example sources that ship inside the package."""

from __future__ import annotations

import pytest

from llvm_lens import cli as cli_mod
from llvm_lens import sample, settings as settings_mod
from llvm_lens.cli import main, read_settings
from llvm_lens.compile import compile_to_ir
from llvm_lens.settings import Settings
from tests.conftest import SAMPLE_C


def fake_build(tmp_path, monkeypatch) -> dict:
    """Stub the build out; return the dict it was called with."""
    seen: dict = {}

    def fake_report(source, **kwargs):
        seen.update(kwargs, source=source)
        return {
            "reportDir": str(tmp_path), "manifest": "m", "laneACount": 0,
            "laneBCount": 0, "totalTimeMs": 0.0, "optCrashed": False,
            "llcCrashed": False, "aiEmbedded": False,
        }

    monkeypatch.setattr(cli_mod, "build_report", fake_report)
    return seen


# --- what ships ------------------------------------------------------------------


def test_every_advertised_sample_is_a_real_source_file():
    assert sample.available(), "the package must ship at least one sample"
    for name in sample.available():
        source = sample.path(name)
        assert source.is_file(), f"{name} is advertised but missing"
        assert source.suffix == ".c"


def test_the_name_is_the_file_stem():
    """So a new sample is a dropped-in .c, not a new entry in a list."""
    assert set(sample.available()) == {
        p.stem for p in sample.directory().iterdir() if p.suffix == ".c"
    }


def test_the_default_is_among_them():
    assert sample.DEFAULT in sample.available()


def test_the_names_are_sorted():
    assert list(sample.available()) == sorted(sample.available())


def test_the_help_lists_the_samples():
    text = cli_mod.build_parser().format_help()
    for name in sample.available():
        assert name in text


@pytest.mark.parametrize("name", sample.available())
def test_every_sample_compiles(name, toolchain, tmp_path):
    """A broken sample would otherwise ship, since it is only ever read."""
    result = compile_to_ir(sample.path(name), out_dir=tmp_path / name, toolchain=toolchain)
    assert result.kind == "clang"
    assert "define" in result.ir_path.read_text()


# --- the command line ---------------------------------------------------------------


def test_an_unknown_sample_is_rejected_listing_the_names(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--sample", "nope"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    for name in sample.available():
        assert name in err


def test_a_sample_and_a_source_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(SAMPLE_C), "--sample"])
    assert excinfo.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_a_sample_refuses_an_explicit_config(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--sample", "--config", "somewhere.yml"])
    assert excinfo.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_a_sample_reads_no_settings_file(tmp_path, monkeypatch):
    """A demo of the defaults must not be rewritten by a file it never named."""
    monkeypatch.delenv(settings_mod.SETTINGS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "llvm-lens.yml").write_text("passes: default<O3>\n")
    parser = cli_mod.build_parser()
    assert read_settings(parser.parse_args(["--sample"])) == Settings()
    # Without the flag the same file is found, so this test can fail.
    assert read_settings(parser.parse_args([str(SAMPLE_C)])).passes == "default<O3>"


# --- the wiring ----------------------------------------------------------------------


def test_the_build_gets_the_packaged_source(tmp_path, monkeypatch):
    seen = fake_build(tmp_path, monkeypatch)
    out = tmp_path / "out"
    assert main(["--sample", "register-pressure", "-o", str(out)]) == 0
    assert seen["source"] == sample.path("register-pressure")
    assert seen["output"] == str(out)
    assert seen["config_file"] is None


def test_a_bare_sample_picks_the_default_and_says_so(tmp_path, monkeypatch, capsys):
    seen = fake_build(tmp_path, monkeypatch)
    assert main(["--sample"]) == 0
    assert seen["source"] == sample.path(sample.DEFAULT)
    out = capsys.readouterr().out
    assert f"sample:     {sample.DEFAULT}" in out
    assert "config:" not in out


def test_the_flags_still_reach_a_sample_build(tmp_path, monkeypatch):
    seen = fake_build(tmp_path, monkeypatch)
    assert main(["--sample", "--passes", "default<O1>", "--opt-arg=-debug"]) == 0
    assert seen["passes"] == "default<O1>"
    assert seen["opt_args"] == ("-debug",)
