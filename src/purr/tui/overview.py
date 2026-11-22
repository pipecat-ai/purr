"""What a PR is about, to decide whether to review it: description, diff, CI.

Details and diffs come from GitHub on demand (the PR list's polls stay lean),
so the UI keeps them in a :class:`DetailsCache` and fetches ahead of the cursor.
"""

import re
import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from purr.diff import File, parse
from purr.models import PR, PRDetails
from purr.tui.theme import CODE_THEME

# CI moves on without the PR's updated_at changing, so details also expire.
STALE_AFTER = 120
# Lines of diff shown at most, files opened; `d` opens all of it.
DIFF_LINES = 3000
# A diff this short shows opened; a longer one starts as its list of files.
SHORT_DIFF = 60
# Diffs kept in memory (they only change with the PR's head).
DIFFS_KEPT = 50

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
        self._diffs: dict[str, tuple[str, str]] = {}  # ref -> (head sha, diff), oldest first
        self._fetching_diffs: set[str] = set()

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

    def diff(self, pr: PR) -> str | None:
        """The PR's diff at its current head, if fetched."""
        head, diff = self._diffs.get(str(pr.ref), ("", ""))
        return diff if head == pr.head_sha else None

    def claim_diffs(self, prs: list[PR]) -> list[PR]:
        """The PRs whose diff is worth fetching, marked as on their way."""
        claimed = [
            pr for pr in prs if self.diff(pr) is None and str(pr.ref) not in self._fetching_diffs
        ]
        self._fetching_diffs.update(str(pr.ref) for pr in claimed)
        return claimed

    def put_diff(self, pr: PR, diff: str | None) -> bool:
        """Store a fetched diff (None: the fetch failed). Returns whether it's new."""
        self._fetching_diffs.discard(str(pr.ref))
        if diff is None:
            return False
        self._diffs.pop(str(pr.ref), None)
        self._diffs[str(pr.ref)] = (pr.head_sha, diff)
        while len(self._diffs) > DIFFS_KEPT:
            del self._diffs[next(iter(self._diffs))]
        return True


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


def opens_short(diff: str | None) -> set[str]:
    """The files that start opened: all of a short diff's, none of a longer one's."""
    if diff is None or len(diff.splitlines()) > SHORT_DIFF:
        return set()
    return {file.path for file in parse(diff)}


def file_line(file: File, opened: bool) -> Text:
    """``▸ src/bus.py  +22 −4``: a file in the diff, opened or not (click to switch)."""
    return Text.assemble(
        ("▾ " if opened else "▸ ", "dim"),
        (file.path, "bold"),
        (f"  +{file.additions}", "green"),
        (f" −{file.deletions}", "red"),
        (f"  {file.note}" if file.note else "", "dim italic"),
    )


def file_path(line: str) -> str | None:
    """The path in a file's line (``▸ src/bus.py  +22 −4``), if it is one."""
    marker, _, rest = line.strip().partition(" ")
    return rest.split("  ")[0] if marker in ("▸", "▾") and rest else None


# How git colors a diff's lines, by how they start.
LINE_STYLES = {"@@": "cyan", "+": "green", "-": "red", "\\": "dim"}


def render_diff(diff: str, opened: Collection[str] = ()) -> RenderableType:
    """The diff's files, a line each; those ``opened`` with their changes, colored like
    ``git diff``, up to DIFF_LINES of them."""
    parts: list[RenderableType] = []
    room = DIFF_LINES
    for file in parse(diff):
        parts.append(file_line(file, file.path in opened))
        if file.path in opened:
            parts.append(_changes(file, room))
            room -= len(file.lines)
    return Group(*parts)


def _changes(file: File, room: int) -> Text:
    """A file's lines, colored; a generated or too long file's only say why not."""
    if file.generated:
        return Text("  generated, not shown", style="dim")
    if len(file.lines) > room:
        return Text("  too long to show here: d opens it", style="dim")
    return Text("\n").join(
        Text(line.text, style=LINE_STYLES.get(line.kind, "")) for line in file.lines
    )


def render_overview(
    details: PRDetails | None,
    error: str | None = None,
    diff: str | None = None,
    diff_error: str | None = None,
    opened: Collection[str] = (),
) -> RenderableType:
    """The description, then the diff: its files, those ``opened`` with their changes."""
    if details is None:
        if error:
            return Text(f"Couldn't load the description: {error}", style="red")
        return Text("Loading the description…", style="dim")
    body = _HTML_COMMENTS.sub("", details.body).strip()
    files = f"{details.changed_files} file{'s' if details.changed_files != 1 else ''} changed"
    parts: list[RenderableType] = [
        Markdown(body, code_theme=CODE_THEME)
        if body
        else Text("No description.", style="dim italic"),
        Text(""),
        Text.assemble(
            (files, "bold"),
            (f"  +{details.additions}", "green"),
            (f" −{details.deletions}", "red"),
            ("  click a file to open it · e opens all · d to read and comment", "dim"),
        ),
    ]
    parts += [_diff_or_why_not(diff, diff_error, opened), Text("")]
    return Group(*parts)


def _diff_or_why_not(
    diff: str | None, error: str | None, opened: Collection[str]
) -> RenderableType:
    if diff is not None:
        return render_diff(diff, opened)
    if error:
        return Text(f"Couldn't load the diff: {error}", style="red")
    return Text("Loading the diff…", style="dim")
