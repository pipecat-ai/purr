"""Purr's bus vocabulary: worker names, job names, events and commands.

Everything in Purr talks over one Pipecat worker bus:

- **Jobs** are requests handled by one worker (``@job(name=...)`` handlers),
  dispatched with ``request_job()`` / ``job()``. Their payloads carry PRs as
  ``owner/repo#123`` strings (see :class:`purr.models.PRRef`).
- **Events** (:class:`PurrEvent`) are broadcast, so the TUI, the voice agent and
  anything else listening all see the same thing. Most are also recorded in the
  activity log (see ``recorded``).
- **Commands** (:class:`PurrCommand`) are targeted at the worker that executes
  them.

Fields stay plain JSON-friendly types so the messages can cross a network bus
if components ever move to separate processes.
"""

from dataclasses import dataclass, field, fields
from typing import ClassVar

from pipecat.bus import BusDataMessage, BusMessage

# Worker names (bus addresses).
GITHUB_WORKER = "github"
REVIEW_WORKER = "reviews"
RECAP_WORKER = "recap"

# Job names handled by RECAP_WORKER.
# payload: {"since": ISO time} -> {"recap_id": int | None} (None: nothing notable happened)
RECAP_CREATE = "recap.create"

# Job names handled by GITHUB_WORKER. A request that can't be done (bad name,
# unknown repo) still completes, with {"error": "..."} as the response.
REPO_ADD = "repo.add"  # payload: {"repo": "owner/name" or a GitHub URL} -> {"repo": "owner/name"}
REPO_REMOVE = "repo.remove"  # payload: {"repo": "owner/name"} -> {"repo": "owner/name"}
# payload: {"action_id": "..."} -> {"url": "...", "warnings": [...]}
ACTION_APPROVE = "action.approve"
ACTION_DISMISS = "action.dismiss"  # payload: {"action_id": "..."} -> {"ok": true}
PR_ASSIGN = "pr.assign"  # payload: {"pr": "owner/repo#123"} -> {"ok": true}: assigns you

# Job names handled by REVIEW_WORKER.
REVIEW_START = "review.start"  # payload: {"pr": "owner/repo#123"}
REVIEW_MESSAGE = "review.message"  # payload: {"pr": "owner/repo#123", "text": "..."}
# Answer an agent's permission request: {"request_id": "...", "allow": bool} -> {"ok": true}
PERMISSION_ANSWER = "session.answer"


@dataclass(kw_only=True)
class PurrEvent(BusDataMessage):
    """Base for Purr's broadcast events.

    Class attributes:
        kind: Stable name, used as the activity log's ``kind``.
        recorded: Whether the event goes into the activity log.
    """

    kind: ClassVar[str]
    recorded: ClassVar[bool] = True


@dataclass(kw_only=True)
class PRListUpdated(PurrEvent):
    """Watched repos, PRs or their states changed; re-read the store."""

    kind: ClassVar[str] = "pr.list_updated"
    recorded: ClassVar[bool] = False


@dataclass(kw_only=True)
class PRChanged(PurrEvent):
    """The poller noticed something about a PR (see :class:`purr.models.PRChange`)."""

    kind: ClassVar[str] = "pr.changed"
    pr: str
    change: str
    title: str


@dataclass(kw_only=True)
class ReviewStarted(PurrEvent):
    kind: ClassVar[str] = "review.started"
    pr: str


@dataclass(kw_only=True)
class ReviewProgress(PurrEvent):
    kind: ClassVar[str] = "review.progress"
    recorded: ClassVar[bool] = False
    pr: str
    text: str


@dataclass(kw_only=True)
class SessionMessage(PurrEvent):
    """One transcript entry from a PR's agent session (text, tool call, tool result...).

    ``message`` is a plain dict with a ``kind`` (see ``purr.review.agent.transcript_entries``).
    """

    kind: ClassVar[str] = "session.message"
    recorded: ClassVar[bool] = False
    pr: str
    message: dict


@dataclass(kw_only=True)
class PermissionRequest(PurrEvent):
    """A PR's agent wants to do something that isn't pre-approved, and waits for you."""

    kind: ClassVar[str] = "session.permission_request"
    pr: str
    request_id: str
    tool: str
    description: str
    reason: str = ""


@dataclass(kw_only=True)
class PermissionResolved(PurrEvent):
    """A permission request was answered, or nobody answered in time (``answered`` false)."""

    kind: ClassVar[str] = "session.permission_resolved"
    pr: str
    request_id: str
    allowed: bool
    answered: bool = True
    description: str = ""


@dataclass(kw_only=True)
class ReviewFinished(PurrEvent):
    kind: ClassVar[str] = "review.finished"
    pr: str
    summary: str
    findings: list[dict] = field(default_factory=list)


@dataclass(kw_only=True)
class ReviewFailed(PurrEvent):
    kind: ClassVar[str] = "review.failed"
    pr: str
    error: str


@dataclass(kw_only=True)
class ActionPending(PurrEvent):
    kind: ClassVar[str] = "action.pending"
    pr: str
    action_id: str
    description: str


@dataclass(kw_only=True)
class ActionResolved(PurrEvent):
    kind: ClassVar[str] = "action.resolved"
    pr: str
    action_id: str
    approved: bool


@dataclass(kw_only=True)
class UserActive(PurrEvent):
    """The user did something (a key, a voice turn). Sent at most about once a minute."""

    kind: ClassVar[str] = "user.active"
    recorded: ClassVar[bool] = False


@dataclass(kw_only=True)
class RecapReady(PurrEvent):
    kind: ClassVar[str] = "recap.ready"
    recorded: ClassVar[bool] = False
    recap_id: int
    headline: str


@dataclass(kw_only=True)
class PurrCommand(BusDataMessage):
    """Base for Purr's targeted commands."""


_BASE_FIELDS = {f.name for f in fields(BusMessage)}


def payload(message: BusMessage) -> dict:
    """A message's own fields (everything but the bus envelope)."""
    return {f.name: getattr(message, f.name) for f in fields(message) if f.name not in _BASE_FIELDS}


def describe(message: BusMessage) -> str:
    """One-line rendering of a message and its payload fields, for logs."""
    args = ", ".join(f"{key}={value!r}" for key, value in payload(message).items())
    target = f" → {message.target}" if message.target else ""
    return f"{type(message).__name__}({args}) from {message.source}{target}"
