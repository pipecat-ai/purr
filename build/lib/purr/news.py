"""What happens to PRs, said once, as news to you.

Every event Purr records in the activity log has a kind (``pr.changed``,
``review.finished``, ...) and its fields as data. Here each kind says how it
reads as news, if it's news at all: the bold titles, the alerts, ``ctrl+n``.
The TUI's live alerts and the news that survives restarts (from the activity
log) both come from here, so they say the same.

A new kind of event, or of PR change: one entry in ``_EVENTS`` or ``_CHANGES``.
"""

from collections.abc import Callable

from purr.models import PRChange, Severity

# How each kind of PR change reads as news; ``{by}`` is who did it (someone, if nobody
# said). None: in the activity log, but not news.
_CHANGES: dict[PRChange, str | None] = {
    PRChange.ASSIGNED: "assigned to you",
    PRChange.UNASSIGNED: None,
    PRChange.POOLED: None,
    PRChange.UNPOOLED: None,
    PRChange.NEW_COMMITS: None,
    PRChange.CLOSED: None,
    PRChange.MERGED: None,
    PRChange.REQUESTED: "asks for your review",
    PRChange.REPLIED: "{by} replied",
}


def _change_news(data: dict) -> str | None:
    try:
        told = _CHANGES[PRChange(data.get("change"))]
    except ValueError:
        return None  # a change this version doesn't know
    return told.format(by=data.get("by") or "someone") if told else None


def _review_news(data: dict) -> str:
    issues = sum(f.get("severity") == Severity.ISSUE for f in data.get("findings") or [])
    plural = "s" if issues != 1 else ""
    return f"review: found {issues} issue{plural}" if issues else "review: nothing found"


# How each kind of event reads as news; the kinds not here never are.
_EVENTS: dict[str, Callable[[dict], str | None]] = {
    "pr.changed": _change_news,
    "review.finished": _review_news,
    "review.failed": lambda data: "review failed",
    "session.permission_request": lambda data: "needs your OK",
}

# The kinds of event that can be news, for reading the activity log.
NEWS_KINDS = tuple(_EVENTS)


def news(kind: str, data: dict) -> str | None:
    """How an event reads as news to you ("alice replied"); None if it isn't news."""
    told = _EVENTS.get(kind)
    return told(data) if told else None
