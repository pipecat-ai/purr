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
    REQUESTED = "requested"  # someone asked for your review (doesn't start one)
    REPLIED = "replied"  # someone answered you on a PR you reviewed


class ActionKind(StrEnum):
    """A GitHub-visible action the agent may propose but never execute itself."""

    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"
    APPROVE = "approve"
    MERGE = "merge"
    PUSH = "push"  # the commits it made in the PR's worktree, to the PR's branch

    @property
    def verdict(self) -> bool:
        """Approving or requesting changes: your verdict on the PR, after which it's done."""
        return self in (ActionKind.APPROVE, ActionKind.REQUEST_CHANGES)


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


@dataclass(frozen=True)
class Verdict:
    """Your latest verdict on a PR on GitHub (approved it, or requested changes), and the
    commit you gave it at: given there or posted by Purr, as Purr last knew."""

    kind: ActionKind  # APPROVE or REQUEST_CHANGES
    head: str


@dataclass(frozen=True)
class Reply:
    """Something said to you on a PR after your last word there (see ``GitHub.standing``).

    ``where`` is ``path:line`` for a reply in a review thread, "" on the PR itself.
    """

    author: str
    body: str
    at: str
    url: str = ""
    where: str = ""


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
    # The author's relation to the repo, as GitHub says: OWNER, MEMBER, COLLABORATOR,
    # CONTRIBUTOR, FIRST_TIME_CONTRIBUTOR, NONE, ... ("" until a poll says).
    author_association: str = ""
    assignees: tuple[str, ...] = ()
    assigned_to_me: bool = False
    requested_from_me: bool = False  # your review is requested (by name, not as a team)
    involved: bool = False  # you've commented on it or reviewed it on GitHub
    state: PRState = PRState.NOT_REVIEWED
    gh_state: GHState = GHState.OPEN
    model: str | None = None  # you chose it for the PR's conversation (/model)
    state_at: str | None = None  # when it got to its state (done: when you finished)
    # Replies to you you haven't answered (or said need no answer), oldest first.
    replies: tuple[Reply, ...] = ()
    verdict: Verdict | None = None  # your latest on GitHub, as Purr last knew
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
class PRHead:
    """Where a PR's commits live: its branch, in its repo (a fork, often)."""

    repo: str | None  # owner/name; None once that repo is gone (a deleted fork)
    branch: str
    sha: str
    maintainer_can_modify: bool = False  # the author lets the base repo's maintainers push


@dataclass(frozen=True)
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class UpNext:
    """A PR in the pool worth your looking at next (see purr.upnext), and why."""

    pr: PRRef
    why: str
    rank: int  # 0 first
    by: str  # "lead", or "purr" when the lead couldn't say


@dataclass(frozen=True)
class PastPR:
    """A PR you wrote (merged) or reviewed, for what you know of a repo (see purr.upnext)."""

    author: str
    paths: tuple[str, ...]  # the files it changed (the first 50)
    yours: bool  # you wrote it; else you reviewed it


@dataclass(frozen=True)
class PRComment:
    """What's said on a PR itself, not on a line: a comment, or a review's text."""

    id: str  # GitHub's node id
    kind: str  # "comment", or "review" (a review's text, which can't be changed here)
    author: str
    body: str
    at: str  # when (ISO)
    mine: bool = False
    state: str = ""  # a review's: APPROVED, CHANGES_REQUESTED, COMMENTED


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
    conversation: tuple[PRComment, ...] = ()  # what's said on it (not on lines), oldest first


class CheckState(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    RUNNING = "running"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Check:
    """One CI check on a PR's head: a GitHub Actions run, or a status (readthedocs)."""

    name: str
    state: CheckState
    required: bool = False  # by the base branch's rules
    started: str = ""  # when it started (ISO), if GitHub says
    finished: str = ""  # when it finished (ISO), if it did and GitHub says


@dataclass(frozen=True)
class MergeCheck:
    """Where a PR stands for merging, as GitHub says now (see :mod:`purr.merging`).

    ``mergeable`` is ``MERGEABLE``, ``CONFLICTING`` or ``UNKNOWN``; ``merge_state``
    is GitHub's ``mergeStateStatus`` (``CLEAN``, ``BLOCKED``, ``BEHIND``, ``DIRTY``,
    ``UNSTABLE``, ...). ``method`` is your default in its repo (merge, squash or
    rebase), what GitHub's own page picks for you. ``reviews`` is each reviewer's
    latest approval or change request (``APPROVED``, ``CHANGES_REQUESTED``).
    """

    head_sha: str
    base: str
    method: str
    mergeable: str | None = None
    merge_state: str | None = None
    checks: tuple[Check, ...] = ()
    assignees: tuple[str, ...] = ()
    reviews: tuple[tuple[str, str], ...] = ()  # (login, state)


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
    up_next: int = 0  # PRs in the pool worth looking at next
    needs_you: int = 0
    to_merge: int = 0  # PRs you approved as they are, still open
    reviewing: int = 0
    not_reviewed: int = 0
    last_activity: str | None = None


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
        by: ``lead`` when the lead planned it, ``default`` when it couldn't,
            ``comments`` to answer the comments on your own PR (not a review).
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
    """What a finished review found, by severity: the one thing to know about it.

    Only issues count as having found something; suggestions and nits are
    mentioned, but a review with nothing else found nothing.
    """

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
    def found(self) -> bool:
        return self.issues > 0

    def text(self) -> str:
        """``found 1 issue, 2 nits``, ``nothing found (1 suggestion)`` or ``nothing found``."""
        parts = [
            f"{n} {name}{'s' if n != 1 else ''}"
            for n, name in [
                (self.issues, "issue"),
                (self.suggestions, "suggestion"),
                (self.nits, "nit"),
            ]
            if n
        ]
        if self.found:
            return "found " + ", ".join(parts)
        return f"nothing found ({', '.join(parts)})" if parts else "nothing found"


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


@dataclass(frozen=True)
class DraftComment:
    """An inline comment you wrote in Purr, posted with your next review of the PR.

    ``side`` is GitHub's: ``RIGHT`` for a line in the PR's version, ``LEFT`` for one
    it deletes (``line`` is then its number in the base). ``head_sha``: the commit
    whose diff you wrote it on. On several lines, ``start_line`` (on ``start_side``)
    is the first, and ``line`` the last.
    """

    id: int
    path: str
    line: int
    side: str
    body: str
    head_sha: str
    start_line: int | None = None
    start_side: str | None = None
    created_at: str = ""

    def for_github(self) -> dict:
        comment = {"path": self.path, "line": self.line, "side": self.side, "body": self.body}
        if self.start_line and (self.start_line, self.start_side) != (self.line, self.side):
            comment |= {"start_line": self.start_line, "start_side": self.start_side}
        return comment


@dataclass(frozen=True)
class Quote:
    """Lines of a PR's diff, to ask about or comment on: in ``path``, from ``start`` (on
    ``start_side``) to ``end`` (on ``side``), numbered as GitHub does (see DraftComment),
    and ``text``, as they read in the diff."""

    path: str
    start: int
    start_side: str
    end: int
    side: str
    text: str

    def ref(self) -> str:
        """``llm_service.py:404–412``: the file's name and its lines."""
        lines = f"{self.start}–{self.end}" if self.start != self.end else str(self.end)
        return f"{self.path.rpartition('/')[2]}:{lines}"

    def lines(self) -> str:
        """``lines 404–412``, saying which version's numbers they are."""
        span = f"lines {self.start}–{self.end}" if self.start != self.end else f"line {self.end}"
        if self.start_side == self.side:
            version = "the PR's version" if self.side == "RIGHT" else "the base (deleted)"
        else:
            version = f"the base's line {self.start} to the PR's line {self.end}"
        return f"{span}, in {version}"


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
