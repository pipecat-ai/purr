"""Purr's bus vocabulary: worker names, job names, events and commands.

Everything in Purr talks over one Pipecat worker bus:

- **Jobs** are requests handled by one worker (``@job(name=...)`` handlers),
  dispatched with ``request_job()`` / ``job()``. Their payloads carry PRs as
  ``owner/repo#123`` strings (see :class:`purr.models.PRRef`).
- **Events** (:class:`PurrEvent`) are broadcast, so the TUI, the voice agent and
  anything else listening all see the same thing.
- **Commands** (:class:`PurrCommand`) are targeted at the worker that executes
  them.

Fields stay plain JSON-friendly types so the messages can cross a network bus
if components ever move to separate processes.
"""

from dataclasses import dataclass, field, fields

from pipecat.bus import BusDataMessage, BusMessage

# Worker names (bus addresses).
GITHUB_WORKER = "github"
REVIEW_WORKER = "reviews"

# Job names, handled by REVIEW_WORKER.
REVIEW_START = "review.start"  # payload: {"pr": "owner/repo#123"}
REVIEW_MESSAGE = "review.message"  # payload: {"pr": "owner/repo#123", "text": "..."}


@dataclass(kw_only=True)
class PurrEvent(BusDataMessage):
    """Base for Purr's broadcast events."""


@dataclass(kw_only=True)
class PRListUpdated(PurrEvent):
    """The set of PRs or their states changed; re-read the store."""


@dataclass(kw_only=True)
class ReviewStarted(PurrEvent):
    pr: str


@dataclass(kw_only=True)
class ReviewProgress(PurrEvent):
    pr: str
    text: str


@dataclass(kw_only=True)
class ReviewFinished(PurrEvent):
    pr: str
    summary: str
    findings: list[dict] = field(default_factory=list)


@dataclass(kw_only=True)
class ReviewFailed(PurrEvent):
    pr: str
    error: str


@dataclass(kw_only=True)
class ActionPending(PurrEvent):
    pr: str
    action_id: str
    description: str


@dataclass(kw_only=True)
class ActionResolved(PurrEvent):
    pr: str
    action_id: str
    approved: bool


@dataclass(kw_only=True)
class PurrCommand(BusDataMessage):
    """Base for Purr's targeted commands."""


@dataclass(kw_only=True)
class ApproveAction(PurrCommand):
    action_id: str


@dataclass(kw_only=True)
class DismissAction(PurrCommand):
    action_id: str


_BASE_FIELDS = {f.name for f in fields(BusMessage)}


def describe(message: BusMessage) -> str:
    """One-line rendering of a message and its payload fields, for logs."""
    payload = ", ".join(
        f"{f.name}={getattr(message, f.name)!r}"
        for f in fields(message)
        if f.name not in _BASE_FIELDS
    )
    target = f" → {message.target}" if message.target else ""
    return f"{type(message).__name__}({payload}) from {message.source}{target}"
