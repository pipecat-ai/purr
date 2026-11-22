"""What happens to PRs, said once: as news to you, and to the recap agent.

Every event Purr records in the activity log has a kind (``pr.changed``,
``review.finished``, ...) and its fields as data. Here each kind says how it
reads as news (if it's news at all: the bold titles, the alerts, ``ctrl+n``), how
the recap agent is told about it, and whether a recap mentions it. The TUI's
live alerts, the news that survives restarts (from the activity log) and the
recaps all come from here, so they say the same.

A new kind of event, or of PR change: one entry in ``_EVENTS`` or ``_CHANGES``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from purr.models import Activity, PRChange, Severity


@dataclass(frozen=True)
class _Change:
    """How a kind of PR change is told; ``{by}`` is who did it (someone, if nobody said)."""

    recap: str
    news: str | None = None  # None: in the log, but not news
    notable: bool = True  # a recap mentions it


_CHANGES = {
    PRChange.ASSIGNED: _Change("assigned to the user", news="assigned to you"),
    PRChange.UNASSIGNED: _Change("no longer assigned to the user"),
    PRChange.POOLED: _Change("new PR in the pool"),
    # Taken by someone, or closed unreviewed: noise in a recap.
    PRChange.UNPOOLED: _Change("left the pool", notable=False),
    PRChange.NEW_COMMITS: _Change("new commits pushed"),
    PRChange.CLOSED: _Change("closed"),
    PRChange.MERGED: _Change("merged"),
    PRChange.REQUESTED: _Change("asks for the user's review", news="asks for your review"),
    PRChange.REPLIED: _Change("{by} replied to the user", news="{by} replied"),
}


def _change(data: dict) -> _Change | None:
    try:
        return _CHANGES[PRChange(data.get("change"))]
    except ValueError:
        return None  # a change this version doesn't know


def _told(text: str, data: dict) -> str:
    return text.format(by=data.get("by") or "someone")


def _change_news(data: dict) -> str | None:
    change = _change(data)
    return _told(change.news, data) if change and change.news else None


def _change_recap(data: dict) -> str:
    change = _change(data)
    return _told(change.recap, data) if change else str(data.get("change"))


def _change_notable(data: dict) -> bool:
    change = _change(data)
    return bool(change and change.notable)


def _review_news(data: dict) -> str:
    issues = sum(f.get("severity") == Severity.ISSUE for f in data.get("findings") or [])
    plural = "s" if issues != 1 else ""
    return f"review: found {issues} issue{plural}" if issues else "review: nothing found"


def _review_recap(data: dict) -> str:
    findings = "; ".join(f"{f.get('severity')}: {f.get('text')}" for f in data.get("findings", []))
    told = f"review finished: {data.get('summary', '')}"
    return f"{told} Findings: {findings}" if findings else told


def _resolved_recap(data: dict) -> str:
    return (
        "user approved the proposed action" if data.get("approved") else "proposed action dismissed"
    )


@dataclass(frozen=True)
class _Event:
    """How a kind of event is told: to the recap agent, as news, and whether a recap says it."""

    recap: Callable[[dict], str]
    news: Callable[[dict], str | None] | None = None  # None: never news
    notable: Callable[[dict], bool] = lambda data: True


_EVENTS = {
    "pr.changed": _Event(_change_recap, news=_change_news, notable=_change_notable),
    "review.finished": _Event(_review_recap, news=_review_news),
    "review.failed": _Event(
        lambda data: f"review failed: {data.get('error', '')}", news=lambda data: "review failed"
    ),
    "action.pending": _Event(lambda data: f"agent proposes: {data.get('description', '')}"),
    "action.resolved": _Event(_resolved_recap),
    "session.permission_request": _Event(
        lambda data: f"agent waiting for permission to: {data.get('description', '')}",
        news=lambda data: "needs your OK",
    ),
    "session.permission_resolved": _Event(
        lambda data: f"nobody answered about: {data.get('description', '')}; agent carried on",
        notable=lambda data: not data.get("answered", True),  # nobody was there to answer
    ),
}

# The kinds of event that can be news, for reading the activity log.
NEWS_KINDS = tuple(kind for kind, event in _EVENTS.items() if event.news)


def news(kind: str, data: dict) -> str | None:
    """How an event reads as news to you ("alice replied"); None if it isn't news."""
    event = _EVENTS.get(kind)
    return event.news(data) if event and event.news else None


def is_notable(activity: Activity) -> bool:
    """Whether a recap mentions it: review progress and pool comings and goings don't."""
    event = _EVENTS.get(activity.kind)
    return bool(event and event.notable(activity.data))


def recap_text(activity: Activity) -> str:
    """What happened, for the recap agent (and the recap's plain list)."""
    event = _EVENTS.get(activity.kind)
    return event.recap(activity.data) if event else activity.kind
