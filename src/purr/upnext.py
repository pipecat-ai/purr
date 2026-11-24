"""Up next: the PRs in a repo's pool worth your looking at next.

The lead picks them (see :mod:`purr.workers.upnext`): a few PRs nobody has taken,
with why. What it goes on is worked out here, without asking anyone:

- how big each PR is: lines and files, from Purr's clone;
- whether it's where you've worked: your merged PRs and the ones you reviewed
  there touched its folders (from GitHub, a few times a day);
- whether you've reviewed its author's PRs before;
- how long it's waited.

:func:`shortlist` orders the pool by those, each with its reasons in words; the
lead chooses from the top of it. If it can't, the top of the shortlist is what's
up next, its reasons joined.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from purr.models import PR, FileChange, GHState, PastPR, PRState

SMALL = 100  # lines: a PR this size or smaller says so
LONG_WAIT = 7  # days: a PR waiting this long says so


@dataclass(frozen=True)
class Known:
    """What you know of a repo: how many of your PRs (written or reviewed) touched each
    folder, and how many of each author's PRs you reviewed (by lowercase login)."""

    folders: Counter[str] = field(default_factory=Counter)
    authors: Counter[str] = field(default_factory=Counter)

    @classmethod
    def of(cls, past: Iterable[PastPR]) -> "Known":
        folders: Counter[str] = Counter()
        authors: Counter[str] = Counter()
        for pr in past:
            folders.update({_folder(path) for path in pr.paths})  # once per PR
            if not pr.yours:
                authors[pr.author.lower()] += 1
        return cls(folders, authors)


@dataclass(frozen=True)
class Signals:
    """What makes a PR worth looking at next, or not."""

    pr: PR
    lines: int  # added and removed
    files: int
    known_folder: str  # its folder you've worked in most ("" if none)
    known: int  # how many of your PRs touched that folder
    by_known: int  # how many of its author's PRs you've reviewed
    waited: float  # days since it was opened


def candidates(prs: Iterable[PR]) -> list[PR]:
    """The PRs up next can come from: open, in the pool, not reviewed, and not asking for
    your review (those have a group of their own)."""
    return [
        pr
        for pr in prs
        if pr.in_pool
        and pr.state == PRState.NOT_REVIEWED
        and pr.gh_state == GHState.OPEN
        and not pr.requested_from_me
    ]


def signals(pr: PR, changes: list[FileChange], known: Known, now: datetime) -> Signals:
    """The PR's signals, from the files it changes (``changes``) and what you know."""
    folders = {_folder(change.path) for change in changes}
    known_folder = max(folders, key=lambda folder: known.folders[folder], default="")
    return Signals(
        pr=pr,
        lines=sum(change.additions + change.deletions for change in changes),
        files=len(changes),
        known_folder=known_folder if known.folders[known_folder] else "",
        known=known.folders[known_folder],
        by_known=known.authors[pr.author.lower()],
        waited=_days_since(pr.created_at, now),
    )


def score(s: Signals) -> float:
    """Higher: more worth looking at next. Small and where you've worked count most; then
    an author whose PRs you've reviewed, and having waited long."""
    small = 1 / (1 + s.lines / 60)
    familiar = min(s.known, 10) / 10
    author = min(s.by_known, 5) / 5
    waited = min(s.waited, 14) / 14
    return 2 * small + 2 * familiar + author + waited / 2


def reasons(s: Signals) -> list[str]:
    """Why it's worth looking at, in a few words each (none: nothing stands out)."""
    said = []
    if s.lines <= SMALL:
        said.append(f"small ({s.lines} line{'s' if s.lines != 1 else ''})")
    if s.known:
        said.append(f"in {_short(s.known_folder)}, where you've been in {s.known} PRs")
    if s.by_known:
        said.append(f"you've reviewed {s.by_known} of {s.pr.author}'s PRs")
    if s.waited >= LONG_WAIT:
        said.append(f"waiting {int(s.waited)} days")
    return said


def why(s: Signals) -> str:
    """Its reasons in a line: what's up next says, if the lead can't."""
    said = reasons(s)
    if said:
        return "; ".join(said).capitalize()
    return f"{s.lines} lines in {s.files} file{'s' if s.files != 1 else ''}"


def shortlist(all_signals: Iterable[Signals], size: int = 15) -> list[Signals]:
    """The most worth looking at, best first."""
    return sorted(all_signals, key=score, reverse=True)[:size]


def _folder(path: str) -> str:
    """The folder a file is in ("." at the top)."""
    return str(PurePosixPath(path).parent)


def _short(folder: str) -> str:
    """A folder's last two parts (``services/deepgram``), or "the top folder"."""
    return "/".join(PurePosixPath(folder).parts[-2:]) if folder != "." else "the top folder"


def _days_since(timestamp: str, now: datetime) -> float:
    if not timestamp:
        return 0.0
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return max((now - then).total_seconds() / 86400, 0.0)
