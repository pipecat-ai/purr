"""What a PR is about, to decide whether to review it: description, files, CI.

Details come from GitHub on demand (the PR list's polls stay lean), so the UI
keeps them in a :class:`DetailsCache` and fetches ahead of the cursor.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from purr.models import PR, PRDetails

# CI moves on without the PR's updated_at changing, so details also expire.
STALE_AFTER = 120
FILES_SHOWN = 30

_HTML_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)  # PR templates are full of them

_CHECKS = {
    "SUCCESS": ("✓ checks pass", "green"),
    "FAILURE": ("✗ checks fail", "red"),
    "ERROR": ("✗ checks fail", "red"),
    "PENDING": ("⟳ checks running", "yellow"),
    "EXPECTED": ("⟳ checks running", "yellow"),
}
_DECISIONS = {
    "APPROVED": ("approved", "green"),
    "CHANGES_REQUESTED": ("changes requested", "yellow"),
}


def short_age(timestamp: str | None) -> str:
    """'now', '5m', '3h', '4d', '3w', '5mo', '2y'."""
    if not timestamp:
        return ""
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    seconds = (datetime.now(UTC) - then).total_seconds()
    for unit, size in (("y", 365 * 86400), ("mo", 30 * 86400), ("w", 7 * 86400), ("d", 86400)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 60)}m" if seconds >= 60 else "now"


@dataclass
class _Entry:
    details: PRDetails
    updated_at: str  # the PR's, when these were fetched
    fetched: float


class DetailsCache:
    """PR details by ``owner/repo#123``, with what's being fetched right now."""

    def __init__(self):
        self._entries: dict[str, _Entry] = {}
        self._fetching: set[str] = set()

    def get(self, pr: PR) -> PRDetails | None:
        entry = self._entries.get(str(pr.ref))
        return entry.details if entry else None

    def _stale(self, pr: PR) -> bool:
        entry = self._entries.get(str(pr.ref))
        return (
            entry is None
            or entry.updated_at != pr.updated_at
            or time.monotonic() - entry.fetched > STALE_AFTER
        )

    def claim(self, prs: list[PR]) -> list[PR]:
        """The PRs worth fetching (stale, and not already on their way), marked as on their way."""
        claimed = [pr for pr in prs if str(pr.ref) not in self._fetching and self._stale(pr)]
        self._fetching.update(str(pr.ref) for pr in claimed)
        return claimed

    def put(self, pr: PR, details: PRDetails | None) -> bool:
        """Store what a fetch brought (None: nothing). Returns whether anything changed."""
        self._fetching.discard(str(pr.ref))
        if details is None:
            return False
        old = self._entries.get(str(pr.ref))
        self._entries[str(pr.ref)] = _Entry(details, pr.updated_at, time.monotonic())
        return old is None or old.details != details


def details_line(pr: PR, details: PRDetails | None) -> Text:
    """``by alice · opened 3d ago · main ← fix-bus · +22 −4 · ✓ checks pass · bug``"""
    text = Text(f"by {pr.author}", style="dim")
    if pr.created_at:
        text.append(f" · opened {short_age(pr.created_at)} ago", style="dim")
    if pr.assigned_to_me:
        text.append(" · assigned to you", style="magenta")
    elif pr.assignees:
        text.append(f" · {', '.join(pr.assignees)} on it", style="cyan")
    if details is None:
        return text
    if details.base:
        text.append(f" · {details.base} ← {details.head}", style="dim")
    text.append(" · ", style="dim")
    text.append(f"+{details.additions}", style="green")
    text.append(f" −{details.deletions}", style="red")
    badges = (_CHECKS.get(details.checks or ""), _DECISIONS.get(details.review_decision or ""))
    for label, style in filter(None, badges):
        text.append(" · ", style="dim")
        text.append(label, style=style)
    if details.labels:
        text.append(f" · {', '.join(details.labels)}", style="dim")
    return text


def _files(details: PRDetails) -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    grid.add_column(justify="right", style="green")
    grid.add_column(justify="right", style="red")
    for change in details.files[:FILES_SHOWN]:
        grid.add_row(change.path, f"+{change.additions}", f"−{change.deletions}")
    return grid


def render_overview(details: PRDetails | None, error: str | None = None) -> RenderableType:
    """The description, then the files changed."""
    if details is None:
        if error:
            return Text(f"Couldn't load the description: {error}", style="red")
        return Text("Loading the description…", style="dim")
    body = _HTML_COMMENTS.sub("", details.body).strip()
    parts: list[RenderableType] = [
        Markdown(body) if body else Text("No description.", style="dim italic"),
        Text(""),
        Text.assemble(
            (
                f"{details.changed_files} file{'s' if details.changed_files != 1 else ''} changed",
                "bold",
            ),
            ("  d full diff", "dim"),
        ),
        _files(details),
    ]
    if (more := details.changed_files - min(len(details.files), FILES_SHOWN)) > 0:
        parts.append(Text(f"… and {more} more", style="dim"))
    parts.append(Text(""))
    return Group(*parts)
