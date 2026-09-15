"""The build settings file: discovery, parsing, the template, and the CLI's use of it."""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest
import yaml

from llvm_lens import cli as cli_mod
from llvm_lens import settings as settings_mod
from llvm_lens.cli import apply_flags, build_parser, main, read_settings
from llvm_lens.settings import (
    FLAT_SPEC, SCHEMA, Settings, SettingsError, Ui, find_config, load_settings,
    template, write_template,
)
from tests.conftest import SAMPLE_C


@pytest.fixture(autouse=True)
def no_ambient_config(tmp_path, monkeypatch):
    """No test may pick up a real llvm-lens.yml from the developer's tree."""
    monkeypatch.delenv(settings_mod.SETTINGS_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write(tmp_path: Path, text: str, name: str = "llvm-lens.yml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# --- defaults and the file ------------------------------------------------------


def test_no_file_means_the_built_in_defaults(tmp_path):
    assert find_config() is None
    settings = load_settings()
    assert settings == Settings()
    assert settings.passes == "default<O2>"
    assert settings.ui == Ui()
    assert settings.config_file is None


def test_every_schema_key_is_read(tmp_path):
    """One file that sets everything, so the mapping cannot silently rot."""
    path = write(tmp_path, """
output: out
passes: 'default<O1>'
custom-passes: [mba-add]
source-map: false
open: true
ai: false
timeout: 12.5
llvm:
  bin-dir: /opt/llvm/bin
  version: 21
  target: aarch64-linux-gnu
plugins:
  dir: build
  pass: [libA.so]
  legacy: [libB.so]
flags:
  clang: [-DFOO=1]
  opt: [-enable-new-pm]
  llc: ['-O3']
ui:
  lane: mir
  mode: cfg
  analysis: ddg
  orientation: stack
  split-ratio: 0.7
  drawer: false
  drawer-tab: Spills
  changed-only: true
  flow-both-lanes: false
""")
    settings = load_settings(path)
    assert settings.output == "out"
    assert settings.passes == "default<O1>"
    assert settings.custom_passes == ("mba-add",)
    assert settings.source_map is False
    assert settings.open_report is True
    assert settings.ai is False
    assert settings.timeout == 12.5
    assert settings.bin_dir == "/opt/llvm/bin"
    assert settings.llvm_version == 21
    assert settings.target == "aarch64-linux-gnu"
    assert settings.pass_plugins == (str(tmp_path / "build" / "libA.so"),)
    assert settings.legacy_plugins == (str(tmp_path / "build" / "libB.so"),)
    assert settings.clang_args == ("-DFOO=1",)
    assert settings.opt_args == ("-enable-new-pm",)
    assert settings.llc_args == ("-O3",)
    assert settings.config_file == str(path)
    assert settings.ui == Ui(
        lane="mir", mode="cfg",
        analysis=("ddg",), orientation="stack", split_ratio=0.7, drawer=False,
        drawer_tab="Spills", changed_only=True, flow_both_lanes=False,
    )


def test_a_partial_file_leaves_the_rest_at_their_defaults(tmp_path):
    settings = load_settings(write(tmp_path, "passes: mem2reg\n"))
    assert settings.passes == "mem2reg"
    assert settings.output == "report"
    assert settings.ui == Ui()


def test_an_empty_file_is_all_defaults(tmp_path):
    path = write(tmp_path, "")
    assert load_settings(path) == Settings(config_file=str(path))


def test_a_section_with_nothing_under_it_is_not_an_error(tmp_path):
    """Commenting out every entry of a section is a normal way to write one."""
    path = write(tmp_path, """
llvm:
  # bin-dir: /usr/lib/llvm-22/bin
  # version: 22
flags:
ui:
""")
    settings = load_settings(path)
    assert settings.bin_dir is None
    assert settings.llvm_version is None
    assert settings.ui == Ui()


def test_a_lone_plugin_name_resolves_against_the_config_directory(tmp_path):
    """So a committed config works from any working directory."""
    nested = tmp_path / "deep" / "nested"
    nested.mkdir(parents=True)
    path = write(tmp_path, "plugins:\n  pass: [libA.so]\n")
    assert load_settings(path).pass_plugins == (str(tmp_path / "libA.so"),)
    assert load_settings(path).pass_plugins == load_settings(path).pass_plugins


def test_an_absolute_plugin_path_is_left_alone(tmp_path):
    path = write(tmp_path, "plugins:\n  pass: [/elsewhere/libA.so]\n")
    assert load_settings(path).pass_plugins == ("/elsewhere/libA.so",)


def test_plugins_dir_catches_bare_names_only(tmp_path):
    path = write(tmp_path, """
plugins:
  dir: build
  pass: [libA.so, sub/libB.so]
""")
    assert load_settings(path).pass_plugins == (
        str(tmp_path / "build" / "libA.so"),
        str(tmp_path / "sub" / "libB.so"),
    )


def test_bin_dir_resolves_against_the_config_too(tmp_path):
    path = write(tmp_path, "llvm:\n  bin-dir: toolchain/bin\n")
    assert load_settings(path).bin_dir == str(tmp_path / "toolchain" / "bin")


# --- discovery ------------------------------------------------------------------


def test_config_is_found_by_walking_up(tmp_path):
    write(tmp_path, "passes: mem2reg\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_config(deep) == tmp_path / "llvm-lens.yml"


def test_the_nearest_file_wins(tmp_path):
    write(tmp_path, "passes: outer\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    write(inner, "passes: inner\n")
    assert find_config(inner) == inner / "llvm-lens.yml"


def test_the_user_config_is_the_last_resort(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    user = xdg / "llvm-lens" / "config.yml"
    user.parent.mkdir(parents=True)
    user.write_text("passes: from-user-config\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert find_config(tmp_path) == user


def test_the_environment_names_the_file(tmp_path, monkeypatch):
    path = write(tmp_path, "passes: from-env\n", name="elsewhere.yml")
    monkeypatch.setenv(settings_mod.SETTINGS_ENV, str(path))
    assert find_config() == path
    assert load_settings().passes == "from-env"


def test_an_environment_path_that_is_missing_is_an_error(tmp_path, monkeypatch):
    """Explicitly named but absent is a mistake, not a prompt to fall back."""
    monkeypatch.setenv(settings_mod.SETTINGS_ENV, str(tmp_path / "gone.yml"))
    with pytest.raises(SettingsError, match="does not exist"):
        load_settings()


def test_all_three_project_filenames_are_accepted(tmp_path):
    for name in settings_mod.PROJECT_FILENAMES:
        file = write(tmp_path, "passes: mem2reg\n", name=name)
        assert find_config(tmp_path) == file
        file.unlink()


# --- which analysis graphs the report opens on ----------------------------------


def graphs(tmp_path, text: str) -> tuple[str, ...]:
    return load_settings(write(tmp_path, f"ui:\n  analysis: {text}\n")).ui.analysis


def test_a_bare_graph_name_is_still_accepted(tmp_path):
    """The old single-value form has to keep working; configs in the wild use it."""
    assert graphs(tmp_path, "pdt") == ("pdt",)


def test_all_is_the_per_function_graphs_and_not_the_call_graph(tmp_path):
    """cg is module-wide, so it would repeat identically under every function."""
    assert graphs(tmp_path, "all") == (
        "pdt", "cdg", "ddg", "pdg", "mdg", "lnt",
    )
    assert "cg" not in graphs(tmp_path, "all")


def test_the_call_graph_is_opt_in(tmp_path):
    assert graphs(tmp_path, "[cg, pdt]") == ("pdt", "cg")
    assert graphs(tmp_path, "cg") == ("cg",)


def test_several_graphs_are_kept_in_the_order_the_chips_are_drawn(tmp_path):
    """So the pane and the chip row cannot disagree about the running order."""
    assert graphs(tmp_path, "[ddg, pdt]") == ("pdt", "ddg")
    assert graphs(tmp_path, "[lnt, mdg, cdg]") == ("cdg", "mdg", "lnt")


def test_a_repeated_graph_name_is_kept_once(tmp_path):
    assert graphs(tmp_path, "[pdt, pdt, cdg]") == ("pdt", "cdg")



# --- rejecting what cannot be used ----------------------------------------------


@pytest.mark.parametrize("text, message", [
    ("passess: mem2reg", "unknown setting 'passess'"),
    ("ui:\n  modes: diff", "unknown setting 'ui.modes'"),
    ("ui:\n  mode: graph", "ui.mode must be one of"),
    ("source-map: sometimes", "source-map must be true or false"),
    ("timeout: soon", "timeout must be a number"),
    ("llvm:\n  version: '22'", "llvm.version must be a whole number"),
    ("custom-passes: {a: 1}", "custom-passes must be a list"),
    ("ui: 3", "'ui' is a section"),
    ("llvm: [22]", "'llvm' is a section"),
    ("- a\n- b", "top level must be a mapping"),
    ("output:\n  - a", "output must be a single value"),
    ("ui:\n  analysis: [pdt, nope]", "ui.analysis must be one of"),
    ("ui:\n  analysis: []", "ui.analysis must name at least one graph"),
    ("ui:\n  analysis: {pdt: 1}", "ui.analysis must be a list"),
])
def test_a_bad_file_is_rejected_with_a_usable_message(tmp_path, text, message):
    with pytest.raises(SettingsError) as excinfo:
        load_settings(write(tmp_path, text))
    assert message in str(excinfo.value)
    assert str(tmp_path / "llvm-lens.yml") in str(excinfo.value)


def test_an_unknown_key_suggests_the_real_one(tmp_path):
    with pytest.raises(SettingsError, match="did you mean passes"):
        load_settings(write(tmp_path, "passess: mem2reg\n"))


def test_invalid_yaml_says_so(tmp_path):
    with pytest.raises(SettingsError, match="not valid YAML"):
        load_settings(write(tmp_path, "passes: [unclosed\n"))


def test_a_missing_file_named_explicitly_is_rejected(tmp_path):
    with pytest.raises(SettingsError, match="no such config file"):
        load_settings(tmp_path / "absent.yml")


# --- the starter file -----------------------------------------------------------


def test_the_template_covers_every_key():
    """The template is the documentation, so it must not drift from the schema."""
    parsed = yaml.safe_load(template())
    assert set(parsed) == set(SCHEMA)
    for key, spec in SCHEMA.items():
        if isinstance(spec, dict):
            assert set(parsed[key]) == set(spec)
        else:
            assert parsed[key] == spec.default or spec.default is None
    assert set(FLAT_SPEC) == {
        f"{k}.{s}" if isinstance(v, dict) else k
        for k, v in SCHEMA.items()
        for s in (v if isinstance(v, dict) else [None])
    }


def test_the_template_documents_every_key():
    text = template()
    for path, spec in FLAT_SPEC.items():
        assert f"# {spec.help}" in text, path
        assert f"{path.rsplit('.', 1)[-1]}:" in text, path


def test_the_template_loads_back_as_the_defaults(tmp_path):
    """What it shows is what you already get."""
    written = write_template(tmp_path / "starter.yml")
    loaded = load_settings(written)
    # Paths are resolved, so compare against the same file's own defaults.
    assert loaded.passes == Settings().passes
    assert loaded.ui == Settings().ui
    assert loaded.source_map == Settings().source_map


def test_write_template_refuses_to_clobber(tmp_path):
    target = tmp_path / "mine.yml"
    target.write_text("passes: mem2reg\n")
    with pytest.raises(SettingsError, match="already exists"):
        write_template(target)
    assert target.read_text() == "passes: mem2reg\n"


def test_write_template_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "llvm-lens.yml"
    assert write_template(target).is_file()


def test_the_checked_in_sample_stays_loadable():
    """The mba-add example ships one; a schema change must not rot it."""
    sample = Path(__file__).parent.parent / "examples" / "mba-add" / "llvm-lens.yml"
    settings = load_settings(sample)
    assert settings.custom_passes == ("mba-add",)
    assert [Path(p).name for p in settings.pass_plugins] == ["libMBAAdd.so"]
    # Resolved against the sample's own directory, which is what makes the
    # plugin loadable -- the loader does not search PATH.
    assert Path(settings.pass_plugins[0]).is_absolute()
    assert Path(settings.pass_plugins[0]).parent == sample.parent
    assert settings.ai is False  # a sample must not ship credentials
    assert settings.ui.changed_only is True


def test_the_sample_only_uses_keys_the_schema_knows():
    """Stated directly, so a typo names itself instead of failing a load."""
    sample = Path(__file__).parent.parent / "examples" / "mba-add" / "llvm-lens.yml"
    known = set(FLAT_SPEC)
    for key, value in yaml.safe_load(sample.read_text()).items():
        if isinstance(value, dict):
            for sub in value:
                assert f"{key}.{sub}" in known, f"{key}.{sub}"
        elif value is not None:  # a section whose entries are all commented out
            assert key in known, key


ROOT_SAMPLE = Path(__file__).parent.parent / "llvm-lens.sample.yml"


def test_the_root_sample_uses_real_keys():
    """It is hand-written, so a stale key must fail here, not at a user's run."""
    raw = yaml.safe_load(ROOT_SAMPLE.read_text())
    for key, value in raw.items():
        if isinstance(value, dict):
            for sub in value:
                assert f"{key}.{sub}" in set(FLAT_SPEC), f"{key}.{sub}"
        else:
            assert key in set(FLAT_SPEC), key


def test_the_root_sample_changes_nothing():
    """Every key sits at its default, so adopting the file is a no-op."""
    settings = load_settings(ROOT_SAMPLE)
    # config_file is the only field that may differ: it records the origin.
    assert dc_replace(settings, config_file=None) == Settings()


def test_the_root_sample_is_not_picked_up_by_walking_up():
    """Named *.sample.yml on purpose: committing it must not change any run."""
    assert ROOT_SAMPLE.name not in settings_mod.PROJECT_FILENAMES


# --- what the flags do with all that --------------------------------------------


def parse(*argv: str):
    return apply_flags(read_settings(build_parser().parse_args(list(argv))),
                       build_parser().parse_args(list(argv)))


def read_through(path: Path, *argv: str) -> Settings:
    """The settings `llvm-lens <argv> --config path` would build with."""
    args = build_parser().parse_args([*argv, "--config", str(path)])
    return apply_flags(read_settings(args), args)


def test_a_flag_beats_the_file(tmp_path):
    path = write(tmp_path, "passes: default<O1>\noutput: from-file\n")
    settings = read_through(path, "--passes", "mem2reg")
    assert settings.passes == "mem2reg"
    assert settings.output == "from-file"  # what the flag did not name, the file keeps


def test_the_repeatable_flags_add_to_the_file(tmp_path):
    """Standing plugins live in the config; a one-off adds to them."""
    path = write(tmp_path, """
plugins:
  pass: [libA.so]
  legacy: [libL.so]
custom-passes: [mba-add]
flags:
  clang: [-DFROM_FILE=1]
  opt: [-from-file]
""")
    # `=` form: argparse reads a leading dash as the next flag, and every
    # argument worth passing to an LLVM tool starts with one.
    settings = read_through(
        path, "--load-pass-plugin", "libB.so", "--load", "libM.so",
        "--custom-pass", "other", "--clang-arg=-DCLI=1", "--opt-arg=-from-cli",
    )
    names = [Path(p).name for p in settings.pass_plugins]
    assert names == ["libA.so", "libB.so"]
    assert [Path(p).name for p in settings.legacy_plugins] == ["libL.so", "libM.so"]
    assert settings.custom_passes == ("mba-add", "other")
    assert settings.clang_args == ("-DFROM_FILE=1", "-DCLI=1")
    assert settings.opt_args == ("-from-file", "-from-cli")


def test_no_config_ignores_the_file(tmp_path):
    write(tmp_path, "passes: default<O1>\n")
    args = build_parser().parse_args(["--no-config", "x.c"])
    assert read_settings(args) == Settings()


def test_a_boolean_flag_carries_both_ways(tmp_path):
    path = write(tmp_path, "source-map: false\nai: false\n")
    assert read_through(path, "x.c").source_map is False
    assert read_through(path, "x.c", "--source-map").source_map is True
    assert read_through(path, "x.c", "--ai").ai is True
    assert read_through(path, "x.c", "--no-ai").ai is False
    # Left alone in the file, they stay off.
    assert read_through(path, "x.c").ai is False


def test_the_target_flag_lands_on_the_setting(tmp_path):
    path = write(tmp_path, "llvm:\n  target: aarch64-linux-gnu\n")
    assert read_through(path, "x.c").target == "aarch64-linux-gnu"
    assert read_through(path, "x.c", "--target", "riscv64").target == "riscv64"


def test_main_refuses_config_and_no_config_together(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(SAMPLE_C), "--config", "a.yml", "--no-config"])
    assert excinfo.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_reports_a_config_it_cannot_read(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(SAMPLE_C), "--config", str(tmp_path / "absent.yml")])
    assert "no such config file" in str(excinfo.value)


def test_a_missing_source_still_says_so(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
    assert "required: source" in capsys.readouterr().err


# --- the command itself ---------------------------------------------------------


def test_init_config_writes_and_exits(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["--init-config"]) == 0
    assert (tmp_path / "llvm-lens.yml").is_file()
    assert "wrote" in capsys.readouterr().out


def test_init_config_takes_a_name(tmp_path, capsys):
    assert main(["--init-config", str(tmp_path / "ci.yml")]) == 0
    assert (tmp_path / "ci.yml").is_file()


def test_init_config_will_not_overwrite(tmp_path, capsys):
    target = tmp_path / "llvm-lens.yml"
    target.write_text("passes: mine\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["--init-config", str(target)])
    assert "already exists" in str(excinfo.value)
    assert target.read_text() == "passes: mine\n"


# --- the flags reach the build ---------------------------------------------------


def test_the_build_gets_the_merged_settings(tmp_path, monkeypatch):
    """The wiring, without paying for a real build."""
    seen = {}

    def fake_report(source, **kwargs):
        seen.update(kwargs)
        seen["source"] = source
        return {
            "reportDir": str(tmp_path), "manifest": "m", "laneACount": 0,
            "laneBCount": 0, "totalTimeMs": 0.0, "optCrashed": False,
            "llcCrashed": False, "aiEmbedded": False,
        }

    monkeypatch.setattr(cli_mod, "build_report", fake_report)
    path = write(tmp_path, """
output: out
passes: default<O1>
llvm:
  target: aarch64-linux-gnu
flags:
  llc: ['-O3']
ui:
  lane: mir
""")
    assert main([str(SAMPLE_C), "--config", str(path), "--opt-arg=-from-cli"]) == 0
    assert seen["passes"] == "default<O1>"
    assert seen["output"] == "out"
    assert seen["target"] == "aarch64-linux-gnu"
    assert seen["llc_args"] == ("-O3",)
    assert seen["opt_args"] == ("-from-cli",)
    assert seen["ui"].lane == "mir"
    assert seen["config_file"] == str(path)


def test_a_config_problem_stops_before_the_build(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the build should not have started")

    monkeypatch.setattr(cli_mod, "build_report", explode)
    with pytest.raises(SystemExit) as excinfo:
        main([str(SAMPLE_C), "--config", str(write(tmp_path, "passess: 1\n"))])
    assert "unknown setting" in str(excinfo.value)


# --- what the report carries -----------------------------------------------------


def test_the_manifest_carries_the_view_defaults(toolchain, tmp_path):
    """The frontend reads metadata.ui at boot; it has to be there and camelCase."""
    from llvm_lens.report import build_report

    out = tmp_path / "report"
    build_report(
        SAMPLE_C, passes="mem2reg", output=out, bin_dir=toolchain.bin_dir,
        source_map=False, ui=Ui(lane="mir", mode="cfg", split_ratio=0.7),
    )
    metadata = json.loads((out / "data" / "manifest.json").read_text())["metadata"]
    assert metadata["ui"] == {
        "lane": "mir", "mode": "cfg",
        "analysis": ["pdt"], "orientation": "side", "splitRatio": 0.7,
        "drawer": True, "drawerTab": "Log", "changedOnly": False,
        "flowBothLanes": True,
    }


def test_extra_tool_flags_reach_the_commands(toolchain, tmp_path):
    from llvm_lens.report import build_report

    out = tmp_path / "report"
    build_report(
        SAMPLE_C, passes="mem2reg", output=out, bin_dir=toolchain.bin_dir,
        source_map=False, llc_args=("-O3",), opt_args=("-S",),
    )
    commands = json.loads((out / "data" / "manifest.json").read_text())["metadata"]["commands"]
    by_stage = {c["stage"]: c["argv"] for c in commands}
    assert "-O3" in by_stage["llc"]
    assert "-S" in by_stage["opt"]


def test_the_target_reaches_the_backend_and_the_metadata(toolchain, tmp_path):
    """A .ll input skips clang, so this needs no cross sysroot.

    clang's half of the same `target` is the `-target <triple>` spelling its
    driver demands (it rejects the GCC-style `--target <triple>`).
    """
    from llvm_lens.report import build_report

    demo = Path(__file__).parent.parent / "examples" / "mba-add" / "demo.ll"
    out = tmp_path / "report"
    build_report(
        demo, output=out, bin_dir=toolchain.bin_dir, source_map=False,
        target="x86_64-unknown-linux-gnu",
    )
    metadata = json.loads((out / "data" / "manifest.json").read_text())["metadata"]
    assert metadata["mtriple"] == "x86_64-unknown-linux-gnu"
    llc = next(c for c in metadata["commands"] if c["stage"] == "llc")
    assert ["-mtriple", "x86_64-unknown-linux-gnu"] == \
        llc["argv"][llc["argv"].index("-mtriple"):llc["argv"].index("-mtriple") + 2]
