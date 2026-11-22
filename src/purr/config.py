"""Runtime configuration: Purr's settings, and where it keeps its files.

Settings come from, each over the one before: their defaults here, your
``settings.toml`` in the config dir (``~/.config/purr``), the environment
(``PURR_DATA_DIR``), and the command line. Each setting's help is next to it
(``_setting``), and the starter ``settings.toml`` is made from it, so the two
can't drift apart.

Where things go follows the XDG convention: what you edit (settings, your
instructions, standing permissions) in the config dir, what Purr manages itself
(the database, its clones, worktrees, the log) in the data dir.
"""

import os
import textwrap
import tomllib
import types
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from pydantic import TypeAdapter, ValidationError

SETTINGS_FILE = "settings.toml"


class SettingsError(ValueError):
    """``settings.toml`` says something Purr can't use; the message says where and what."""


def _config_dir() -> Path:
    if env := os.environ.get("PURR_CONFIG_DIR"):
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "purr"


def _xdg_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "purr"


def _setting(default: Any = MISSING, *, about: str, factory: Any = MISSING) -> Any:
    """A setting, with what it does (for the starter ``settings.toml`` and the docs)."""
    return field(default=default, default_factory=factory, metadata={"about": about})


@dataclass(frozen=True)
class Config:
    """Purr's settings (see the module's docstring for where they come from)."""

    data_dir: Path = _setting(
        about="Where Purr keeps its database, log, clones and worktrees. Default: "
        "$PURR_DATA_DIR, else $XDG_DATA_HOME/purr, else ~/.local/share/purr."
    )
    poll_interval: float = _setting(60.0, about="Seconds between GitHub polls.")
    auto_review: bool = _setting(
        True, about="Review PRs assigned to you without being asked (--no-auto-review: off)."
    )
    review_model: str | None = _setting(
        None, about="Model for reviews and the recap. Empty: Claude Code's default."
    )
    lead_model: str | None = _setting(
        "claude-sonnet-5-5",
        about="Model for the lead, which plans each review (one short call per batch of PRs). "
        "Empty: Claude Code's default.",
    )
    quick_model: str | None = _setting(
        "claude-sonnet-5-5",
        about="Model for reviews the lead planned as quick (and quick re-reviews); your own "
        "messages in a PR's session use review_model. Empty: review_model for those too.",
    )
    review_budget_usd: float | None = _setting(
        None, about="Spending cap per review run, in US dollars. Empty: none."
    )
    away_after: float = _setting(
        15 * 60, about="Seconds without a key after which you're away (the recap's for then)."
    )
    permission_wait: float = _setting(
        5 * 60,
        about="Seconds an agent waits for you to allow something before carrying on without "
        "it. Claude Code gives up on its own after about ten minutes: keep it below that.",
    )
    max_reviews: int = _setting(
        3,
        about="Reviews running at once; the rest wait in line (your own messages in a PR's "
        "session never wait). Also --max-reviews.",
    )
    signature: str = _setting(
        "Sent from [Purr](https://github.com/pipecat-ai/purr).",
        about="Small print at the end of what Purr posts (a review's text, each reply in a "
        "thread), so readers know it came from Purr. Markdown. Empty: none.",
    )
    permission_mode: str = _setting(
        "auto",
        about="Claude Code's permission mode for reviews: auto (safe things run, risky ones "
        "ask), acceptEdits, default, ... Also --permission-mode.",
    )
    sandbox: bool = _setting(
        False,
        about="Run review agents' commands in Claude Code's sandbox (confined to the PR's "
        "checkout and a few domains). Needs bubblewrap on Linux; see the README. Also --sandbox.",
    )
    config_dir: Path = field(default_factory=_config_dir)  # where settings.toml is

    @classmethod
    def load(cls, *, data_dir: Path | None = None, config_dir: Path | None = None) -> "Config":
        """The settings: defaults, then ``settings.toml``, then the environment, then
        ``data_dir`` (from the command line). Raises SettingsError if the file's wrong."""
        config_dir = config_dir or _config_dir()
        settings = read_settings(config_dir / SETTINGS_FILE)
        from_file = settings.pop("data_dir", None)
        data_dir = (
            data_dir
            or (Path(env) if (env := os.environ.get("PURR_DATA_DIR")) else None)
            or from_file
            or _xdg_data_dir()
        )
        return cls(**settings, data_dir=Path(data_dir).expanduser(), config_dir=config_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "purr.db"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "purr.log"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"


# What settings.toml can set: everything but where it is itself.
_SETTINGS = {f.name: f for f in fields(Config) if f.name != "config_dir"}


def read_settings(path: Path) -> dict[str, Any]:
    """The settings in ``path`` (none if it doesn't exist), checked: names and types."""
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"{path}: {e}") from e
    if unknown := sorted(set(raw) - _SETTINGS.keys()):
        raise SettingsError(f"{path}: unknown setting {', '.join(map(repr, unknown))}")
    return {name: _value(path, name, value) for name, value in raw.items()}


def _value(path: Path, name: str, value: Any) -> Any:
    """A setting's value as its type, empty meaning none for those that can be none."""
    kind = _SETTINGS[name].type
    if value == "" and _optional(kind):
        return None
    try:
        return TypeAdapter(kind).validate_python(value, strict=False)
    except ValidationError as e:
        reason = e.errors()[0]["msg"].lower()
        raise SettingsError(f"{path}: {name} = {value!r}: {reason}") from e


def _optional(kind: Any) -> bool:
    return get_origin(kind) in (Union, types.UnionType) and type(None) in get_args(kind)


def settings_template() -> str:
    """The starter ``settings.toml``: every setting, commented out at its default."""
    lines = [
        "# Purr's settings. Uncomment one to change it; Purr reads this when it starts.",
        "# Command-line flags (purr --help) win over what's here.",
        "",
    ]
    for setting in _SETTINGS.values():
        lines += [f"# {line}" for line in textwrap.wrap(setting.metadata["about"], 86)]
        lines += [f"# {setting.name} = {_toml(_default(setting))}", ""]
    return "\n".join(lines)


def _default(setting: Any) -> Any:
    if setting.name == "data_dir":
        return "~/.local/share/purr"
    return setting.default


def _toml(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(value)
