"""What a PR is about, to decide whether to review it: description, diff, CI.

Details and diffs come from GitHub on demand (the PR list's polls stay lean),
so the UI keeps them in a :class:`DetailsCache` and fetches ahead of the cursor.
"""

import posixpath
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text

from purr.models import PR, PRComment, PRDetails
from purr.tui.theme import palette

# CI moves on without the PR's updated_at changing, so details also expire.
STALE_AFTER = 120
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
    """``by alice · opened 3d ago · main ← fix-bus · +22 −4 in 2 files · ✓ checks pass · bug``"""
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
    files = details.changed_files
    text.append(f" in {files} file{'s' if files != 1 else ''}", style="dim")
    badges = (_CHECKS.get(details.checks or ""), _DECISIONS.get(details.review_decision or ""))
    for label, style in filter(None, badges):
        text.append(" · ", style="dim")
        text.append(label, style=style)
    if details.labels:
        text.append(f" · {', '.join(details.labels)}", style="dim")
    return text


def where_it_changes(details: PRDetails) -> str:
    """Where a PR changes things, in a few words: its files, if there are three or fewer;
    else the directories with the most changed lines, and how many files each."""
    files = details.files
    if len(files) <= 3:
        return ", ".join(file.path for file in files)
    lines: Counter[str] = Counter()
    count: Counter[str] = Counter()
    for file in files:
        folder = posixpath.dirname(file.path)
        lines[folder] += file.additions + file.deletions
        count[folder] += 1
    common = posixpath.commonpath(list(lines))
    top = [folder for folder, _ in lines.most_common(3)]
    return "most in " + ", ".join(f"{_shorter(folder, common)} ({count[folder]})" for folder in top)


def _shorter(folder: str, common: str) -> str:
    """A folder without what all of them share (``src/pipecat/``), unless that's all it is."""
    rest = posixpath.relpath(folder, common) if common else folder
    return folder or "." if rest == "." else rest


def render_overview(details: PRDetails | None, error: str | None = None) -> RenderableType:
    """The description, then where the change is (the diff itself is d's)."""
    if details is None:
        if error:
            return Text(f"Couldn't load the description: {error}", style="red")
        return Text("Loading the description…", style="dim")
    body = _HTML_COMMENTS.sub("", details.body).strip()
    files = f"{details.changed_files} file{'s' if details.changed_files != 1 else ''}"
    return Group(
        Markdown(body, code_theme=palette().code)
        if body
        else Text("No description.", style="dim italic"),
        Text(""),
        Text.assemble(
            (f"{files} changed", "bold"),
            (f": {where_it_changes(details)}" if details.files else "", ""),
            ("  · d to read them and comment", "dim"),
        ),
        Text(""),
        *render_conversation(details.conversation),
    )


CONVERSATION_SHOWN = 5  # the latest things said on the PR, under its description

# A review's state, said next to who wrote it.
_VERDICTS = {
    "APPROVED": ("approved", "green"),
    "CHANGES_REQUESTED": ("changes requested", "red"),
    "COMMENTED": ("reviewed", "dim"),
}


def render_conversation(conversation: Sequence[PRComment]) -> list[RenderableType]:
    """What's said on the PR itself, the latest few: who, when, what. Answering, changing
    or deleting is the diff's (d), where all of it is, at the top."""
    if not conversation:
        return []
    out: list[RenderableType] = [
        Text.assemble(
            (f"Conversation ({len(conversation)})", "bold"),
            ("  · d to answer, edit or delete, at the top of the diff", "dim"),
        )
    ]
    if len(conversation) > CONVERSATION_SHOWN:
        out.append(Text(f"  {len(conversation) - CONVERSATION_SHOWN} earlier", style="dim"))
    for comment in conversation[-CONVERSATION_SHOWN:]:
        verdict, color = _VERDICTS.get(comment.state, ("", ""))
        out.append(
            Text.assemble(
                ("│ ", "dim"),
                (comment.author, "bold"),
                (" (you)" if comment.mine else "", "dim"),
                (f" · {short_age(comment.at)}", "dim"),
                (f" · {verdict}" if verdict else "", color),
            )
        )
        out.append(Padding(Markdown(comment.body, code_theme=palette().code), (0, 0, 0, 2)))
    out.append(Text(""))
    return out
