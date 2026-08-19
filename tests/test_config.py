from __future__ import annotations

from pathlib import Path

import pytest

from comdiary.config import (
    CONFIG_ENV,
    HOME_ENV,
    Config,
    candidate_paths,
    config_home,
    deep_merge,
    default_config_path,
    load_config,
)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake $HOME so discovery never touches the developer's real config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.delenv(HOME_ENV, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestDiscovery:
    def test_defaults_when_nothing_exists(self, isolated: Path, tmp_path: Path):
        cfg = load_config(cwd=tmp_path)
        assert cfg.source_path is None
        assert cfg.ledger == (isolated / ".comdiary" / "ledger").resolve()

    def test_home_dotdir(self, isolated: Path, tmp_path: Path):
        write(isolated / ".comdiary" / "config.toml", 'ledger = "/tmp/from-home"\n')
        cfg = load_config(cwd=tmp_path)
        assert cfg.ledger == Path("/tmp/from-home")

    def test_xdg_config_dir(self, isolated: Path, tmp_path: Path):
        write(isolated / ".config" / "comdiary" / "config.toml", 'ledger = "/tmp/from-xdg"\n')
        cfg = load_config(cwd=tmp_path)
        assert cfg.ledger == Path("/tmp/from-xdg")

    def test_cwd_dotdir_beats_home(self, isolated: Path, tmp_path: Path):
        write(isolated / ".comdiary" / "config.toml", 'ledger = "/tmp/from-home"\n')
        write(tmp_path / ".comdiary" / "config.toml", 'ledger = "/tmp/from-cwd"\n')
        assert load_config(cwd=tmp_path).ledger == Path("/tmp/from-cwd")

    def test_bare_toml_beats_cwd_dotdir(self, isolated: Path, tmp_path: Path):
        write(tmp_path / ".comdiary" / "config.toml", 'ledger = "/tmp/from-dotdir"\n')
        write(tmp_path / "comdiary.toml", 'ledger = "/tmp/from-bare"\n')
        assert load_config(cwd=tmp_path).ledger == Path("/tmp/from-bare")

    def test_env_var_wins(self, isolated: Path, tmp_path: Path, monkeypatch):
        write(tmp_path / "comdiary.toml", 'ledger = "/tmp/from-cwd"\n')
        chosen = write(tmp_path / "elsewhere.toml", 'ledger = "/tmp/from-env"\n')
        monkeypatch.setenv(CONFIG_ENV, str(chosen))
        assert load_config(cwd=tmp_path).ledger == Path("/tmp/from-env")

    def test_comdiary_home_env_relocates_the_dotdir(self, isolated: Path, tmp_path: Path, monkeypatch):
        custom = tmp_path / "custom-home"
        monkeypatch.setenv(HOME_ENV, str(custom))
        write(custom / "config.toml", 'ledger = "/tmp/from-custom"\n')
        assert config_home() == custom
        assert load_config(cwd=tmp_path).ledger == Path("/tmp/from-custom")

    def test_comdiary_home_also_relocates_the_default_ledger(
        self, isolated: Path, tmp_path: Path, monkeypatch
    ):
        custom = tmp_path / "custom-home"
        monkeypatch.setenv(HOME_ENV, str(custom))
        assert load_config(cwd=tmp_path).ledger == (custom / "ledger").resolve()

    def test_explicit_missing_path_is_an_error(self, isolated: Path, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.toml")

    def test_candidates_are_unique_and_ordered(self, isolated: Path, tmp_path: Path):
        paths = candidate_paths(tmp_path)
        assert len(paths) == len(set(paths))
        assert paths[0] == tmp_path / "comdiary.toml"
        assert paths[1] == tmp_path / ".comdiary" / "config.toml"
        assert default_config_path() in paths


class TestOverlays:
    def test_conf_d_overrides_base(self, isolated: Path, tmp_path: Path):
        base = write(
            isolated / ".comdiary" / "config.toml",
            'ledger = "/tmp/base"\n[llm]\nbackend = "copilot"\nmodel = "auto"\n',
        )
        write(base.parent / "conf.d" / "10-local.toml", '[llm]\nmodel = "other"\n')
        cfg = load_config(cwd=tmp_path)
        assert cfg.llm.model == "other"
        # Sibling keys in the same table survive — this is a merge, not a replace.
        assert cfg.llm.backend == "copilot"
        assert cfg.ledger == Path("/tmp/base")

    def test_overlays_apply_in_filename_order(self, isolated: Path, tmp_path: Path):
        base = write(isolated / ".comdiary" / "config.toml", '[llm]\nmodel = "a"\n')
        write(base.parent / "conf.d" / "10-x.toml", '[llm]\nmodel = "b"\n')
        write(base.parent / "conf.d" / "20-y.toml", '[llm]\nmodel = "c"\n')
        cfg = load_config(cwd=tmp_path)
        assert cfg.llm.model == "c"
        assert [p.name for p in cfg.overlays] == ["10-x.toml", "20-y.toml"]

    def test_deep_merge_leaves_untouched_tables_alone(self):
        merged = deep_merge(
            {"a": {"x": 1, "y": 2}, "b": 1},
            {"a": {"y": 3}},
        )
        assert merged == {"a": {"x": 1, "y": 3}, "b": 1}


class TestResolution:
    def test_user_paths_expand(self, isolated: Path):
        cfg = Config.model_validate(
            {"ledger": "~/led", "ingest": {"inbox": "~/in", "done": "~/done"}}
        ).resolved()
        assert cfg.ledger == (isolated / "led").resolve()
        assert cfg.ingest.inbox == isolated / "in"

    def test_failed_defaults_next_to_done(self, isolated: Path):
        cfg = Config.model_validate({"ingest": {"done": "/tmp/x/memos_done"}}).resolved()
        assert cfg.ingest.failed == Path("/tmp/x/memos_failed")


def test_example_config_matches_the_builtin_template():
    """examples/ is generated from config.EXAMPLE_TOML — keep them from drifting."""
    from comdiary.config import EXAMPLE_TOML

    example = Path(__file__).resolve().parents[1] / "examples" / "config.example.toml"
    assert example.read_text(encoding="utf-8") == EXAMPLE_TOML


def test_example_config_parses(tmp_path: Path, isolated: Path):
    from comdiary.config import EXAMPLE_TOML

    path = write(tmp_path / "comdiary.toml", EXAMPLE_TOML)
    cfg = load_config(path)
    assert cfg.ingest.inbox is not None
    assert cfg.llm.backend == "copilot"


class TestWindowsPaths:
    r"""A Windows ledger path is the reason paths are written as TOML *literal*
    strings: inside a basic string, "C:\Users" is the escape \U and fails to
    parse with "invalid hex value"."""

    def test_backslash_path_round_trips(self, tmp_path: Path, isolated: Path):
        from comdiary.config import EXAMPLE_TOML, toml_string

        win = r"C:\Users\you\.comdiary\ledger"
        body = EXAMPLE_TOML.replace(
            "ledger = '~/.comdiary/ledger'", f"ledger = {toml_string(win)}"
        )
        cfg = load_config(write(tmp_path / "comdiary.toml", body))
        assert str(cfg.ledger).endswith("ledger")

    def test_toml_string_prefers_a_literal_string(self):
        from comdiary.config import toml_string

        assert toml_string(r"G:\マイドライブ\memos") == r"'G:\マイドライブ\memos'"
        assert toml_string("/home/x/ledger") == "'/home/x/ledger'"

    def test_toml_string_falls_back_when_a_quote_is_present(self):
        import tomllib

        from comdiary.config import toml_string

        rendered = toml_string("/tmp/it's here")
        assert tomllib.loads(f"p = {rendered}")["p"] == "/tmp/it's here"

    def test_broken_toml_names_the_file_and_the_cause(self, tmp_path: Path, isolated: Path):
        from comdiary.config import ConfigError

        path = write(tmp_path / "comdiary.toml", 'ledger = "C:\\Users\\you\\ledger"\n')
        with pytest.raises(ConfigError) as exc:
            load_config(path)
        assert str(path) in str(exc.value)
        assert "シングルクォート" in str(exc.value)
