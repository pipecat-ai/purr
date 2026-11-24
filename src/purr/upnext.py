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

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Protocol

from purr.claude import structured
from purr.models import PR, FileChange, GHState, PastPR, PRRef, PRState, UpNext

SMALL = 100  # lines: a PR this size or smaller says so
# Folders more than this share of your PRs touched (changelog/, tests/) say nothing about
# the code you know, once there are enough PRs to tell (MANY).
COMMON = 1 / 3
MANY = 10
LONG_WAIT = 7  # days: a PR waiting this long says so


@dataclass(frozen=True)
class Known:
    """What you know of a repo: how many of your PRs (written or reviewed) touched each
    folder, and how many of each author's PRs you reviewed (by lowercase login).

    Folders nearly every PR touches (changelog/, tests/), and the top one, are left
    out: they say nothing about the code you know."""

    folders: Counter[str] = field(default_factory=Counter)
    authors: Counter[str] = field(default_factory=Counter)

    @classmethod
    def of(cls, past: Iterable[PastPR]) -> "Known":
        past = list(past)
        folders: Counter[str] = Counter()
        authors: Counter[str] = Counter()
        for pr in past:
            folders.update({_folder(path) for path in pr.paths})  # once per PR
            if not pr.yours:
                authors[pr.author.lower()] += 1
        common = len(past) >= MANY and COMMON * len(past)
        telling = {f: n for f, n in folders.items() if f != "." and not (common and n > common)}
        return cls(Counter(telling), authors)


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
        line = "; ".join(said)
        return line[:1].upper() + line[1:]  # not capitalize(): it'd lowercase "PRs"
    return f"{s.lines} lines in {s.files} file{'s' if s.files != 1 else ''}"


def shortlist(all_signals: Iterable[Signals], size: int = 15) -> list[Signals]:
    """The most worth looking at, best first."""
    return sorted(all_signals, key=score, reverse=True)[:size]


INSTRUCTIONS = """\
You're the lead in Purr, an app that helps the user review GitHub pull requests.
From a repo's open PRs that nobody has taken yet, choose the few the user should
look at next, best first. Each comes with what Purr knows: its size, where its
lines are, whether the user has worked in that code or reviewed the author's PRs
before, and how long it's waited.

Favor PRs the user can review well and quickly: small and straightforward, in
code they know. Having waited long counts a little. Leave out ones that look like
they need someone else (a big feature in code the user has never touched),
unless there's nothing better. The user's own instructions, and what they took
or skipped lately, say what they care about.

For each one you choose, `why`: one short line for the user, specific to the PR
(e.g. "Small fix to Deepgram's reconnect; you've reviewed 8 PRs there."). Choose
at most {count}. Judge from what's given; don't invent details.
"""

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pr": {"type": "string", "description": "#123"},
                    "why": {"type": "string"},
                },
                "required": ["pr", "why"],
            },
        }
    },
    "required": ["picks"],
}

_MAX_INSTRUCTIONS = 3000


def notes(
    repo: str,
    shortlisted: Sequence[Signals],
    *,
    instructions: str = "",
    taken: Sequence[PR] = (),
    skipped: Sequence[PR] = (),
) -> str:
    """What the lead is told to choose from."""
    parts = [f"Repo: {repo}", "Nobody has taken these yet (Purr's best guess first):"]
    parts.append("\n".join(_described(s) for s in shortlisted))
    if taken:
        parts.append("The user lately took:\n" + "\n".join(_titled(pr) for pr in taken))
    if skipped:
        parts.append("The user said these weren't for them:\n" + "\n".join(map(_titled, skipped)))
    if instructions.strip():
        parts.append("The user's own instructions:\n" + instructions.strip()[:_MAX_INSTRUCTIONS])
    return "\n\n".join(parts)


def _described(s: Signals) -> str:
    pr = s.pr
    who = f"{pr.author} ({pr.author_association.lower()})" if pr.author_association else pr.author
    said = [f"{s.lines} lines in {s.files} files", *reasons(s)]
    return f"- #{pr.number} “{pr.title}” by {who}, opened {int(s.waited)} days ago: " + "; ".join(
        said
    )


def _titled(pr: PR) -> str:
    return f"- #{pr.number} “{pr.title}” by {pr.author}"


def picks_from(output: dict, shortlisted: Sequence[Signals], count: int) -> list[UpNext]:
    """The lead's picks, of those it was given, at most ``count``, in its order."""
    offered = {s.pr.number: s.pr.ref for s in shortlisted}
    picks: dict[PRRef, str] = {}
    for pick in output.get("picks") or []:
        number = _number(str(pick.get("pr", "")))
        if number in offered and offered[number] not in picks:
            picks[offered[number]] = str(pick.get("why") or "").strip()
    chosen = list(picks.items())[:count]
    return [UpNext(ref, why, rank, by="lead") for rank, (ref, why) in enumerate(chosen)]


def _number(text: str) -> int | None:
    """The PR number in ``#123``, ``123`` or ``owner/repo#123``."""
    match = re.search(r"(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def fallback(shortlisted: Sequence[Signals], count: int) -> list[UpNext]:
    """Up next when the lead can't say: the top of the shortlist, with its reasons."""
    return [UpNext(s.pr.ref, why(s), rank, by="purr") for rank, s in enumerate(shortlisted[:count])]


class Picker(Protocol):
    async def pick(self, notes: str, count: int) -> dict:
        """The PRs to look at next, of those in ``notes``, as PICK_SCHEMA describes."""
        ...


class ClaudePicker:
    """Chooses with a tool-less Agent SDK query and structured output (the lead's model).

    Args:
        model: Model to use; None uses Claude Code's configured default.
    """

    def __init__(self, *, model: str | None = None):
        self._model = model

    async def pick(self, notes: str, count: int) -> dict:
        return await structured(
            notes,
            instructions=INSTRUCTIONS.format(count=count),
            schema=PICK_SCHEMA,
            model=self._model,
            what="pick",
        )


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
