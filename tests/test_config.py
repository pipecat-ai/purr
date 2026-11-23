"""Purr's settings: defaults, settings.toml, the environment, the command line."""

import tomllib

import pytest

from purr.cli import Flags, _config
from purr.config import SETTINGS_FILE, Config, SettingsError, settings_template


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("PURR_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


def write(config_dir, text: str) -> None:
    (config_dir / SETTINGS_FILE).write_text(text)


def test_without_a_file_the_defaults(dirs, tmp_path):
    config = Config.load(config_dir=dirs)
    assert config.data_dir == tmp_path / "xdg" / "purr"
    assert config.max_reviews == 3 and config.auto_review and config.review_model is None


def test_settings_from_the_file(dirs):
    write(
        dirs,
        """
        max_reviews = 5
        away_after = 600
        lead_model = ""          # Claude Code's default
        review_model = "claude-opus-5-5"
        signature = ""           # unsigned
        review_budget_usd = 2
        """,
    )
    config = Config.load(config_dir=dirs)
    assert config.max_reviews == 5 and config.away_after == 600
    assert config.lead_model is None and config.review_model == "claude-opus-5-5"
    assert config.signature == "" and config.review_budget_usd == 2.0


def test_what_the_file_cant_say(dirs):
    write(dirs, "away_aftr = 600\n")
    with pytest.raises(SettingsError, match="unknown setting 'away_aftr'"):
        Config.load(config_dir=dirs)
    write(dirs, 'max_reviews = "lots"\n')
    with pytest.raises(SettingsError, match="max_reviews = 'lots'"):
        Config.load(config_dir=dirs)
    write(dirs, "max_reviews = \n")
    with pytest.raises(SettingsError, match=SETTINGS_FILE):
        Config.load(config_dir=dirs)
    write(dirs, 'config_dir = "/elsewhere"\n')  # where the file is can't be in it
    with pytest.raises(SettingsError, match="unknown setting 'config_dir'"):
        Config.load(config_dir=dirs)


def test_the_environment_and_the_command_line_win(dirs, tmp_path, monkeypatch):
    write(dirs, 'data_dir = "~/from-file"\nmax_reviews = 5\n')
    monkeypatch.setenv("PURR_CONFIG_DIR", str(dirs))
    assert _config(Flags()).data_dir.name == "from-file"
    monkeypatch.setenv("PURR_DATA_DIR", str(tmp_path / "from-env"))
    assert _config(Flags()).data_dir == tmp_path / "from-env"
    config = _config(Flags(data_dir=tmp_path / "flag", max_reviews=2, auto_review=False))
    assert config.data_dir == tmp_path / "flag" and config.max_reviews == 2
    assert not config.auto_review and not config.sandbox  # unsaid: the file's, or the default


def test_the_starter_file_has_every_setting_at_its_default(dirs):
    template = settings_template()
    assert tomllib.loads(template) == {}  # all commented out: changes nothing
    uncommented = "\n".join(
        line.removeprefix("# ") for line in template.splitlines() if " = " in line
    )
    write(dirs, uncommented)
    config = Config.load(config_dir=dirs)
    defaults = Config(data_dir=config.data_dir, config_dir=dirs)
    assert config == defaults
    assert config.data_dir.name == "purr"  # ~/.local/share/purr, expanded
