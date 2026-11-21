"""Domain types shared by the store, the workers and the UIs."""

import re
from dataclasses import dataclass, field
from enum import StrEnum


class PRState(StrEnum):
    """Where a PR sits in the review flow (the groups in the PR list)."""

    NOT_REVIEWED = "not_reviewed"
    REVIEWING = "reviewing"
    NEEDS_YOU = "needs_you"
    DONE = "done"


class ReviewStatus(StrEnum):
    """Status of one review session.

    A review still ``RUNNING`` when the app starts was interrupted by the last
    shutdown and gets resumed.
    """

    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


class Severity(StrEnum):
    ISSUE = "issue"
    SUGGESTION = "suggestion"
    NIT = "nit"


class ActionKind(StrEnum):
    """A GitHub-visible action the agent may propose but never execute itself."""

    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"
    APPROVE = "approve"
    MERGE = "merge"


class ActionStatus(StrEnum):
    """Lifecycle of a pending action: pending → approved → executed/failed, or dismissed."""

    PENDING = "pending"
    APPROVED = "approved"
    DISMISSED = "dismissed"
    EXECUTED = "executed"
    FAILED = "failed"


_REF_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")


@dataclass(frozen=True, order=True)
class PRRef:
    """Identifies a PR. Its string form ``owner/repo#123`` is what goes on the bus."""

    repo: str
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @classmethod
    def parse(cls, value: str) -> "PRRef":
        match = _REF_RE.match(value.strip())
        if not match:
            raise ValueError(f"not a PR reference (expected owner/repo#123): {value!r}")
        return cls(repo=match["repo"], number=int(match["number"]))


@dataclass
class PR:
    repo: str
    number: int
    title: str
    author: str
    url: str
    head_sha: str
    updated_at: str
    assigned_to_me: bool = False
    state: PRState = PRState.NOT_REVIEWED
    id: int | None = None

    @property
    def ref(self) -> PRRef:
        return PRRef(self.repo, self.number)


@dataclass
class Finding:
    severity: Severity
    text: str
    file: str | None = None
    line: int | None = None
    id: int | None = None


@dataclass
class Review:
    id: int
    pr: PRRef
    status: ReviewStatus
    started_at: str
    session_id: str | None = None
    worktree_path: str | None = None
    summary: str | None = None
    error: str | None = None
    finished_at: str | None = None


@dataclass
class PendingAction:
    id: str
    pr: PRRef
    kind: ActionKind
    description: str
    created_at: str
    payload: dict = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    resolved_at: str | None = None
