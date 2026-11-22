"""Purr's bus vocabulary: worker names, jobs and events.

Everything in Purr talks over one Pipecat worker bus:

- **Jobs** are requests handled by one worker: a :class:`purr.jobs.Request`
  dataclass, answered with its reply dataclass or a :class:`purr.jobs.PurrError`
  (see :mod:`purr.jobs`).
- **Events** (:class:`PurrEvent`) are broadcast, so the TUI, the voice agent and
  anything else listening all see the same thing. Most are also recorded in the
  activity log (see ``recorded``).

Fields stay plain JSON-friendly types so the messages can cross a network bus
if components ever move to separate processes.
"""

from dataclasses import dataclass, field, fields
from typing import ClassVar, Literal

from pipecat.bus import BusDataMessage, BusMessage

from purr.jobs import Done, Request
from purr.models import Depth, MergeCheck, PRDetails, PRRef, ReviewPlan
from purr.review.skills import RepoSkill, Skill

# Worker names (bus addresses).
GITHUB_WORKER = "github"
REVIEW_WORKER = "reviews"
RECAP_WORKER = "recap"
GIT_WORKER = "git"
LEAD_WORKER = "lead"

# -- Jobs (see purr.jobs): each request names the worker that handles it, and its reply.

# The lead (LEAD_WORKER) decides what gets reviewed, how deep and in what order (see
# purr.lead).


@dataclass(kw_only=True)
class Planned:
    started: list[PRRef] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)  # owner/repo#123 -> why not


@dataclass(kw_only=True)
class PlanReviews(Request[Planned]):
    """Have the lead plan these PRs' reviews, and start them. It answers once it has
    the PRs; their reviews start after it has planned them. ``depth`` overrides its."""

    worker: ClassVar[str] = LEAD_WORKER
    timeout: ClassVar[float] = 180
    prs: list[PRRef]
    depth: Depth | None = None


# RECAP_WORKER


@dataclass(kw_only=True)
class RecapMade:
    recap_id: int | None  # None: nothing notable happened


@dataclass(kw_only=True)
class MakeRecap(Request[RecapMade]):
    """Recap what happened since ``since`` (an ISO time)."""

    worker: ClassVar[str] = RECAP_WORKER
    timeout: ClassVar[float] = 180
    since: str


# GITHUB_WORKER


@dataclass(kw_only=True)
class Watching:
    repo: str  # GitHub's spelling of its name


@dataclass(kw_only=True)
class WatchRepo(Request[Watching]):
    worker: ClassVar[str] = GITHUB_WORKER
    repo: str  # owner/name, or its GitHub URL


@dataclass(kw_only=True)
class UnwatchRepo(Request[Done]):
    worker: ClassVar[str] = GITHUB_WORKER
    repo: str


@dataclass(kw_only=True)
class Posted:
    url: str
    warnings: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class ApproveAction(Request[Posted]):
    """Post what the agent proposed, as you."""

    worker: ClassVar[str] = GITHUB_WORKER
    action_id: str


@dataclass(kw_only=True)
class DismissAction(Request[Done]):
    worker: ClassVar[str] = GITHUB_WORKER
    action_id: str


@dataclass(kw_only=True)
class DismissReplies(Request[Done]):
    """The PR's replies to you need no answer (the ones Purr has seen)."""

    worker: ClassVar[str] = GITHUB_WORKER
    pr: PRRef


@dataclass(kw_only=True)
class MergeReadiness:
    """Where a PR stands for merging, fresh from GitHub (see :mod:`purr.merging`)."""

    check: MergeCheck
    blockers: list[str]  # no merging past these
    concerns: list[str]  # merging anyway is your call


@dataclass(kw_only=True)
class CheckMerge(Request[MergeReadiness]):
    """Where the PR stands for merging: the first ``m``."""

    worker: ClassVar[str] = GITHUB_WORKER
    pr: PRRef


@dataclass(kw_only=True)
class Merged:
    """Merged (how: merge, squash or rebase); or not, since something new came up after you
    looked (``readiness``: how it stands now)."""

    merged: bool
    method: str = ""
    readiness: MergeReadiness | None = None


@dataclass(kw_only=True)
class MergePR(Request[Merged]):
    """Merge the PR you approved, at the commit you approved: the second ``m``.

    ``seen`` is the concerns you decided past; one that wasn't there then means
    you look again instead.
    """

    worker: ClassVar[str] = GITHUB_WORKER
    pr: PRRef
    seen: list[str]


@dataclass(kw_only=True)
class AssignMe(Request[Done]):
    worker: ClassVar[str] = GITHUB_WORKER
    pr: PRRef


@dataclass(kw_only=True)
class MarkSeen(Request[Done]):
    """You looked at the PR: what happened so far isn't news anymore."""

    worker: ClassVar[str] = GITHUB_WORKER
    pr: PRRef


@dataclass(kw_only=True)
class FetchedDetails:
    details: dict[str, PRDetails]  # owner/repo#123 -> its details


@dataclass(kw_only=True)
class FetchDetails(Request[FetchedDetails]):
    """Description, files and CI of some PRs, from GitHub."""

    worker: ClassVar[str] = GITHUB_WORKER
    prs: list[PRRef]


# GIT_WORKER


@dataclass(kw_only=True)
class PRDiff:
    diff: str


@dataclass(kw_only=True)
class GetDiff(Request[PRDiff]):
    """A PR's unified diff, as `gh pr diff` shows it."""

    worker: ClassVar[str] = GIT_WORKER
    pr: PRRef


@dataclass(kw_only=True)
class Worktree:
    path: str


@dataclass(kw_only=True)
class GetWorktree(Request[Worktree]):
    """The PR's worktree, made if it's gone (to open it in Claude Code, say)."""

    worker: ClassVar[str] = GIT_WORKER
    timeout: ClassVar[float] = 120
    pr: PRRef


# REVIEW_WORKER


@dataclass(kw_only=True)
class ReviewOpened:
    review_id: int


@dataclass(kw_only=True)
class StartReview(Request[ReviewOpened]):
    worker: ClassVar[str] = REVIEW_WORKER
    pr: PRRef
    plan: ReviewPlan | None = None


@dataclass(kw_only=True)
class Rereviewing:
    started: bool  # False: nothing new to look at


@dataclass(kw_only=True)
class Rereview(Request[Rereviewing]):
    """Have a finished review look at the PR's new commits."""

    worker: ClassVar[str] = REVIEW_WORKER
    pr: PRRef
    plan: ReviewPlan | None = None


@dataclass(kw_only=True)
class MessageSent:
    """What writing did, besides going to the agent (see ReviewWorker.send_followup)."""

    answered: bool = False  # it answered the agent's permission request: a no
    set_aside: list[str] = field(default_factory=list)  # the proposals it set aside
    while_working: bool = False  # the agent takes it up at its next step


@dataclass(kw_only=True)
class SendMessage(Request[MessageSent]):
    """Say something in the PR's conversation."""

    worker: ClassVar[str] = REVIEW_WORKER
    pr: PRRef
    text: str


@dataclass(kw_only=True)
class PermissionAnswered:
    rule: str | None = None  # the rule kept, if any: Bash(uv run pytest:*)


@dataclass(kw_only=True)
class AnswerPermission(Request[PermissionAnswered]):
    """Answer an agent's permission request. ``remember`` keeps Claude Code's suggested
    rule, for the PR's repo or everywhere (see purr.review.rules)."""

    worker: ClassVar[str] = REVIEW_WORKER
    request_id: str
    allow: bool
    remember: Literal["repo", "all"] | None = None


@dataclass(kw_only=True)
class SetModel(Request[Done]):
    """The model for everything in a PR's conversation, from now on (None: Purr's choice)."""

    worker: ClassVar[str] = REVIEW_WORKER
    pr: PRRef
    model: str | None


@dataclass(kw_only=True)
class RepoSkills:
    skills: list[RepoSkill]


@dataclass(kw_only=True)
class ListRepoSkills(Request[RepoSkills]):
    """A repo's skills and commands, as Claude Code names them (for /help and completion)."""

    worker: ClassVar[str] = REVIEW_WORKER
    repo: str


@dataclass(kw_only=True)
class ReviewSkills:
    skills: list[Skill]  # likeliest first
    chosen: str | None = None  # what its reviews follow: '' for Purr's own; None: undecided


@dataclass(kw_only=True)
class ListReviewSkills(Request[ReviewSkills]):
    """A repo's skills that look like they're for reviewing (see purr.review.skills)."""

    worker: ClassVar[str] = REVIEW_WORKER
    repo: str


@dataclass(kw_only=True)
class ChooseReviewSkill(Request[Done]):
    """Have the repo's reviews follow ``skill`` ('' for Purr's own; None: ask next time)."""

    worker: ClassVar[str] = REVIEW_WORKER
    repo: str
    skill: str | None


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
class Synced(PurrEvent):
    """A poll finished: the store matches GitHub (as of now)."""

    kind: ClassVar[str] = "github.synced"
    recorded: ClassVar[bool] = False


@dataclass(kw_only=True)
class PRChanged(PurrEvent):
    """The poller noticed something about a PR (see :class:`purr.models.PRChange`).

    ``by`` says who, when it was someone: who replied to you ("alice, bob").
    """

    kind: ClassVar[str] = "pr.changed"
    pr: str
    change: str
    title: str
    by: str = ""


@dataclass(kw_only=True)
class ReviewStarted(PurrEvent):
    """A review started (it may wait in line); ``plan`` is what the lead decided it needs."""

    kind: ClassVar[str] = "review.started"
    pr: str
    plan: dict = field(default_factory=dict)


@dataclass(kw_only=True)
class ReviewProgress(PurrEvent):
    """What a PR's review is doing now; ``""`` once nothing is.

    ``waiting``: on you, or for its turn; the agent isn't working on it yet.
    """

    kind: ClassVar[str] = "review.progress"
    recorded: ClassVar[bool] = False
    pr: str
    text: str
    waiting: bool = False


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
    """A PR's agent wants to do something that isn't pre-approved, and waits for you.

    ``rule`` is Claude Code's suggested rule, what "always allow" would keep
    ("" if it suggested none).
    """

    kind: ClassVar[str] = "session.permission_request"
    pr: str
    request_id: str
    tool: str
    description: str
    reason: str = ""
    rule: str = ""


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
class SkillQuestion(PurrEvent):
    """A repo has skills that may be for reviewing; should its reviews follow one?

    Its reviews wait a while for an answer (:class:`ChooseReviewSkill`), then go ahead
    without.
    ``skills`` are ``{"name", "description", "likely"}``, likeliest first.
    """

    kind: ClassVar[str] = "review.skill_question"
    recorded: ClassVar[bool] = False
    repo: str
    skills: list[dict] = field(default_factory=list)


@dataclass(kw_only=True)
class SkillChosen(PurrEvent):
    """A repo's reviews follow ``skill`` from now on ('' for Purr's own review).

    ``chosen_by`` is ``purr`` (it was clearly the repo's review skill) or ``you``.
    """

    kind: ClassVar[str] = "review.skill_chosen"
    repo: str
    skill: str
    chosen_by: str


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


_BASE_FIELDS = {f.name for f in fields(BusMessage)}


def payload(message: BusMessage) -> dict:
    """A message's own fields (everything but the bus envelope)."""
    return {f.name: getattr(message, f.name) for f in fields(message) if f.name not in _BASE_FIELDS}


def describe(message: BusMessage, *, width: int = 200) -> str:
    """One-line rendering of a message and its payload fields, for logs.

    Each value is cut to ``width`` characters: a session message carries whole tool
    results, which the session's own transcript already keeps.
    """
    values = payload(message).items()
    args = ", ".join(f"{key}={_short(repr(value), width)}" for key, value in values)
    return f"{type(message).__name__}({args}) from {message.source}"


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else f"{text[: width - 1]}…"
