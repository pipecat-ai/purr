"""Runtime configuration: where Purr keeps its data."""

import os
from dataclasses import dataclass
from pathlib import Path


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
    """

    data_dir: Path
    poll_interval: float = 60.0

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
