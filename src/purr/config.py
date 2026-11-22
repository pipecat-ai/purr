"""Runtime configuration: where Purr keeps its data."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_config_dir() -> Path:
    if env := os.environ.get("PURR_CONFIG_DIR"):
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "purr"


def _default_data_dir() -> Path:
    if env := os.environ.get("PURR_DATA_DIR"):
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "purr"


@dataclass(frozen=True)
class Config:
    """Purr configuration.

    Parameters:
        data_dir: Directory holding the database, logs and review worktrees.
            Defaults to ``$PURR_DATA_DIR``, else ``$XDG_DATA_HOME/purr``, else
            ``~/.local/share/purr``.
        poll_interval: Seconds between GitHub polls.
        auto_review: Review PRs assigned to you without being asked.
        review_model: Model for reviews; None uses Claude Code's default.
        lead_model: Model for the lead, which plans each review (one short call
            per batch of PRs); None uses Claude Code's default.
        quick_model: Model for reviews the lead planned as quick (and quick
            re-reviews); your own messages in the PR's session use
            ``review_model``. None uses ``review_model`` for those too.
        review_budget_usd: Optional spending cap per review run.
        away_after: Seconds without activity after which you count as away.
        max_reviews: Reviews running at once; the rest wait in line (your own
            messages in a PR's session never wait).
        permission_wait: Seconds an agent waits for you to allow something before
            carrying on without it. Claude Code gives up on its own after about
            ten minutes, so keep this below that.
        signature: Small print at the end of what Purr posts (a review's text,
            each reply in a thread), so readers know it came from Purr; empty
            for none.
        config_dir: Directory for files the user edits (review instructions).
            Defaults to ``$PURR_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/purr``,
            else ``~/.config/purr``.
    """

    data_dir: Path
    poll_interval: float = 60.0
    auto_review: bool = True
    review_model: str | None = None
    lead_model: str | None = "claude-sonnet-5-5"
    quick_model: str | None = "claude-sonnet-5-5"
    review_budget_usd: float | None = None
    away_after: float = 15 * 60
    permission_wait: float = 5 * 60
    max_reviews: int = 3
    signature: str = "Sent from Purr."
    config_dir: Path = field(default_factory=_default_config_dir)

    @classmethod
    def load(cls, *, data_dir: Path | None = None) -> "Config":
        return cls(data_dir=(data_dir or _default_data_dir()).expanduser())

    @property
    def db_path(self) -> Path:
        return self.data_dir / "purr.db"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "purr.log"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"
