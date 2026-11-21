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


class GHState(StrEnum):
    """A PR's state on GitHub (as opposed to its place in Purr's review flow)."""

    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class PRChange(StrEnum):
    """Something the poller noticed about a PR."""

    ASSIGNED = "assigned"  # assigned to you
    UNASSIGNED = "unassigned"  # no longer assigned to you
    POOLED = "pooled"  # new in a watched repo's pool, or back in it
    UNPOOLED = "unpooled"  # left the pool (someone else was assigned, or it closed)
    NEW_COMMITS = "new_commits"  # head moved on a PR Purr has reviewed
    CLOSED = "closed"
    MERGED = "merged"


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
_REPO_RE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)?"
    r"(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?$"
)


def parse_repo_name(value: str) -> str:
    """Turn ``owner/name`` or a GitHub URL (https or ssh) into ``owner/name``."""
    match = _REPO_RE.match(value.strip())
    if not match:
        raise ValueError(f"not a GitHub repo (expected owner/name or a GitHub URL): {value!r}")
    return f"{match['owner']}/{match['name']}"


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
    created_at: str = ""
    base: str = ""  # the branch it merges into; "" until a poll says
    assignees: tuple[str, ...] = ()
    assigned_to_me: bool = False
    state: PRState = PRState.NOT_REVIEWED
    gh_state: GHState = GHState.OPEN
    id: int | None = None

    @property
    def ref(self) -> PRRef:
        return PRRef(self.repo, self.number)

    @property
    def in_pool(self) -> bool:
        """Nobody is assigned: free for anyone to review."""
        return not self.assignees and not self.assigned_to_me

    @property
    def others_on_it(self) -> bool:
        """Someone else is assigned and you aren't."""
        return bool(self.assignees) and not self.assigned_to_me


@dataclass(frozen=True)
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class PRDetails:
    """What you look at before deciding to review a PR. Fetched on demand.

    ``checks`` is the head commit's combined CI state as GitHub names it
    (``SUCCESS``, ``FAILURE``, ``PENDING``, ...), or None without CI.
    """

    body: str = ""
    base: str = ""
    head: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    files: tuple[FileChange, ...] = ()
    labels: tuple[str, ...] = ()
    review_decision: str | None = None
    checks: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "PRDetails":
        """From its JSON form (``dataclasses.asdict``), as it crosses the bus."""
        files = tuple(FileChange(**f) for f in data.get("files") or ())
        return cls(**{**data, "files": files, "labels": tuple(data.get("labels") or ())})


@dataclass
class Repo:
    """A watched repo. ``synced_at`` is unset until its first complete poll."""

    name: str
    added_at: str
    synced_at: str | None = None


@dataclass
class RepoSummary:
    """A row of the repo list: a watched repo, or one where a PR concerns you."""

    name: str
    watched: bool
    needs_you: int = 0
    reviewing: int = 0
    not_reviewed: int = 0
    last_activity: str | None = None


@dataclass
class Recap:
    """A "while you were away" summary.

    ``items`` are ``{"pr": "owner/repo#123", "text": "...", "needs_you": bool}``,
    most urgent first.
    """

    id: int
    since: str
    until: str
    headline: str
    created_at: str
    items: list[dict] = field(default_factory=list)


@dataclass
class Activity:
    """One entry in the activity log: a Purr event, as recorded."""

    id: int
    at: str
    kind: str
    pr: PRRef | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Finding:
    severity: Severity
    text: str
    file: str | None = None
    line: int | None = None
    id: int | None = None


class Depth(StrEnum):
    """How deep a review goes (see :class:`ReviewPlan`)."""

    QUICK = "quick"  # one agent: the diff and the code it touches
    FULL = "full"  # the repo's review process, with subagents for independent checks


class Priority(StrEnum):
    """Which reviews go first when they wait in line."""

    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return list(Priority).index(self)


@dataclass(frozen=True)
class ReviewPlan:
    """What Purr's lead decided a PR's review needs (see :mod:`purr.lead`).

    Parameters:
        depth: Quick or full.
        priority: Where it goes in line.
        focus: What the review should look at hardest, specific to the PR.
        why: One line for the user: why this depth and priority.
        by: ``lead`` when the lead planned it, ``default`` when it couldn't.
    """

    depth: Depth = Depth.FULL
    priority: Priority = Priority.NORMAL
    focus: tuple[str, ...] = ()
    why: str = ""
    by: str = "default"

    def to_dict(self) -> dict:
        return {
            "depth": self.depth.value,
            "priority": self.priority.value,
            "focus": list(self.focus),
            "why": self.why,
            "by": self.by,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ReviewPlan":
        """From its JSON form; whatever's missing or invalid takes the default."""
        data = data or {}
        depth = data.get("depth")
        priority = data.get("priority")
        focus = data.get("focus") or ()
        return cls(
            depth=Depth(depth) if depth in Depth._value2member_map_ else Depth.FULL,
            priority=(
                Priority(priority) if priority in Priority._value2member_map_ else Priority.NORMAL
            ),
            focus=tuple(str(f) for f in focus if str(f).strip()) if isinstance(focus, list) else (),
            why=str(data.get("why") or ""),
            by=str(data.get("by") or "default"),
        )


@dataclass(frozen=True)
class Outcome:
    """What a finished review found, by severity: the one thing to know about it."""

    issues: int = 0
    suggestions: int = 0
    nits: int = 0

    @classmethod
    def of(cls, findings) -> "Outcome":
        """From findings (:class:`Finding` or their dicts)."""
        counts = {severity: 0 for severity in Severity}
        for finding in findings:
            severity = finding.get("severity") if isinstance(finding, dict) else finding.severity
            if severity in Severity._value2member_map_:
                counts[Severity(severity)] += 1
        return cls(counts[Severity.ISSUE], counts[Severity.SUGGESTION], counts[Severity.NIT])

    @property
    def found(self) -> int:
        return self.issues + self.suggestions + self.nits

    def text(self) -> str:
        """``nothing found``, or ``found 1 issue, 2 nits``."""
        if not self.found:
            return "nothing found"
        parts = [
            f"{n} {name}{'s' if n != 1 else ''}"
            for n, name in [
                (self.issues, "issue"),
                (self.suggestions, "suggestion"),
                (self.nits, "nit"),
            ]
            if n
        ]
        return "found " + ", ".join(parts)


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
    head_sha: str | None = None  # the commit this review last looked at
    skill: str | None = None  # the repo's own skill it followed, if any
    plan: ReviewPlan | None = None  # what the lead decided it needs


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
