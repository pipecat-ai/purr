"""What a PR is about, to decide whether to review it: description, diff, CI.

Details and diffs come from GitHub on demand (the PR list's polls stay lean),
so the UI keeps them in a :class:`DetailsCache` and fetches ahead of the cursor.
"""

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.rule import Rule
from rich.text import Text

from purr.models import PR, PRDetails
from purr.tui.theme import CODE_THEME

# CI moves on without the PR's updated_at changing, so details also expire.
STALE_AFTER = 120
# Lines of diff shown; `d` opens all of it in a pager.
DIFF_LINES = 3000
# Diffs kept in memory (they only change with the PR's head).
DIFFS_KEPT = 50
# Generated files whose diff is noise: just their name and size.
COLLAPSED = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum", "*.min.js", "*.svg")

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


_FILE_NOTES = {
    "new file mode": "new file",
    "deleted file mode": "deleted",
    "rename from": "renamed from",
    "Binary files": "binary",
}
_SKIPPED = ("index ", "--- ", "+++ ", "old mode", "new mode", "similarity index", "rename to")


def _file_rule(path: str, details: PRDetails | None) -> Rule:
    title = Text(path, style="bold")
    change = next((f for f in details.files if f.path == path), None) if details else None
    if change:
        title.append(f"  +{change.additions}", style="green")
        title.append(f" −{change.deletions}", style="red")
    return Rule(title, align="left", style="dim")


def render_diff(diff: str, details: PRDetails | None = None) -> RenderableType:
    """A unified diff the way ``git diff`` colors it, a rule per file."""
    parts: list[RenderableType] = []
    lines = diff.splitlines()
    hunk: Text | None = None
    collapsed = False
    for line in lines[:DIFF_LINES]:
        if line.startswith("diff --git "):
            path = line.rpartition(" b/")[2]
            parts.append(_file_rule(path, details))
            collapsed = any(fnmatch(path.rpartition("/")[2], pattern) for pattern in COLLAPSED)
            hunk = Text()
            if collapsed:
                hunk.append("generated, not shown\n", style="dim")
            parts.append(hunk)
            continue
        if hunk is None or collapsed or line.startswith(_SKIPPED):
            continue
        note = next((n for prefix, n in _FILE_NOTES.items() if line.startswith(prefix)), None)
        if note:
            rest = line.removeprefix("rename from ") if note == "renamed from" else ""
            hunk.append(f"{note} {rest}".rstrip() + "\n", style="dim italic")
        elif line.startswith("@@"):
            hunk.append(line + "\n", style="cyan")
        elif line.startswith("+"):
            hunk.append(line + "\n", style="green")
        elif line.startswith("-"):
            hunk.append(line + "\n", style="red")
        elif line.startswith("\\"):
            hunk.append(line + "\n", style="dim")
        else:
            hunk.append(line + "\n")
    if (more := len(lines) - DIFF_LINES) > 0:
        parts.append(Text(f"… {more} more lines. d opens the whole diff.", style="dim"))
    return Group(*parts)


def render_overview(
    details: PRDetails | None,
    error: str | None = None,
    diff: str | None = None,
    diff_error: str | None = None,
) -> RenderableType:
    """The description, then the diff."""
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
            ("  d opens it in your pager", "dim"),
        ),
    ]
    if diff is not None:
        parts.append(render_diff(diff, details))
    elif diff_error:
        parts.append(Text(f"Couldn't load the diff: {diff_error}", style="red"))
    else:
        parts.append(Text("Loading the diff…", style="dim"))
    parts.append(Text(""))
    return Group(*parts)
