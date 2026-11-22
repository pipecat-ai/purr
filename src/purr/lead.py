"""Purr's lead: plans each PR's review before it starts.

Every PR assigned to you gets reviewed. The lead decides how: a quick review
for small, low-risk changes, a full one for anything with real risk; what each
review should look at hardest; and which go first. It's one structured call per
batch of PRs, from what Purr already has (description, files, CI, the repo's
skills, your instructions), so it's cheap and holds nothing up for long. If it
can't plan (no network, a bad answer), the PRs get a full review, in order.

The lead worker (:mod:`purr.workers.lead`) hands the plans to the review
worker, which runs them. The rules that matter (nothing posted without you, the
permission policy, how many reviews run at once) stay in code.
"""

from dataclasses import dataclass, field
from typing import Protocol

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from purr.claude import log_stderr
from purr.models import PR, Outcome, PRDetails, Priority, PRRef, Review, ReviewPlan
from purr.review.skills import RepoSkill

INSTRUCTIONS = """\
You're the lead reviewer in Purr, an app that reviews GitHub pull requests for
the user with Claude agents. Every PR you're given will be reviewed; you plan
how. For each one, decide:

- depth: "quick" or "full".
  - quick: small, low-risk changes (docs, comments, typos, version or
    dependency bumps, tests only, small obvious fixes). One agent reads the
    diff and the code it touches.
  - full: anything with real risk (new features, behavior changes,
    concurrency, public APIs, security-sensitive code, core code paths, large
    diffs). The repo's own review process, with subagents for independent
    checks. When in doubt, full.
- priority: "high", "normal" or "low": the order reviews run in.
  High: someone is likely waiting (small fixes, things others build on,
  release or urgent labels, PRs the user asked for). Low: drafts, PRs with
  failing CI, big PRs that will take long anyway.
- focus: up to 4 short points the review should look at hardest, specific to
  the PR (e.g. "the new lock in BusQueue.put: can it deadlock on shutdown?",
  "docstrings follow the repo's conventions: pipecat:prose-review"). Name one
  of the repo's skills when it fits; only those marked as ones the agent can
  load. Empty when nothing stands out.
- why: one short line for the user, e.g. "New STT service, 420 lines, touches
  frame handling." or "Docstring fixes only."

Some PRs are re-reviews: the author pushed new commits after an earlier
review. Plan for what changed, not the whole PR: quick when the new commits
are small follow-ups (addressing review comments, renames, tests), full when
they change a lot or touch risky code. Focus on whether the earlier findings
are resolved, and on what's new.

Judge from what's given; don't invent details. The user's own instructions,
when there are any, say what matters to them.
"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pr": {"type": "string", "description": "owner/repo#123"},
                    "depth": {"type": "string", "enum": ["quick", "full"]},
                    "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                    "focus": {"type": "array", "items": {"type": "string"}},
                    "why": {"type": "string"},
                },
                "required": ["pr", "depth", "priority", "focus", "why"],
            },
        }
    },
    "required": ["plans"],
}

_MAX_FILES = 30
_MAX_BODY = 1500
_MAX_INSTRUCTIONS = 3000
_MAX_FOCUS = 4


@dataclass
class RepoContext:
    """What the lead knows about a repo: its skills, and how you like reviews there."""

    skills: list[RepoSkill] = field(default_factory=list)
    review_skill: str | None = None  # what full reviews follow, if anything
    instructions: str = ""  # yours, for this repo (review and code style)


@dataclass
class LeadInput:
    """One PR to plan, with what's known about it."""

    pr: PR
    details: PRDetails | None = None
    previous: Review | None = None  # its last review, if any
    outcome: Outcome | None = None  # what that review found
    requested: bool = False  # you asked for this review
    rereview: bool = False  # new commits since ``previous``
    new_commits: list[str] | None = None  # their subjects; None: couldn't tell
    new_files: list[str] = field(default_factory=list)  # what they change: "M src/bus.py"


def lead_notes(inputs: list[LeadInput], repos: dict[str, RepoContext]) -> str:
    """The lead's input: per repo, its context, then its PRs."""
    lines: list[str] = []
    for repo in dict.fromkeys(item.pr.repo for item in inputs):
        context = repos.get(repo) or RepoContext()
        lines.append(f"# {repo}")
        if context.review_skill:
            lines.append(f"Full reviews here follow the repo's /{context.review_skill} skill.")
        loadable = [s for s in context.skills if s.model_invocable]
        if loadable:
            lines.append("Skills a review agent can load:")
            lines += [f"- {s.name}: {s.description}" for s in loadable]
        if context.instructions:
            lines.append("The user's instructions:")
            lines.append(context.instructions.strip()[:_MAX_INSTRUCTIONS])
        lines.append("")
        for item in (i for i in inputs if i.pr.repo == repo):
            lines += _pr_notes(item)
    return "\n".join(lines).strip()


def _pr_notes(item: LeadInput) -> list[str]:
    pr, details = item.pr, item.details
    lines = [f'## {pr.ref} "{pr.title}" by {pr.author}']
    facts = []
    if item.requested:
        facts.append("the user asked for this review now")
    if pr.assigned_to_me:
        facts.append("assigned to the user")
    if details:
        facts.append(f"+{details.additions} -{details.deletions} in {details.changed_files} files")
        facts.append(f"CI: {details.checks or 'none'}")
        if details.labels:
            facts.append(f"labels: {', '.join(details.labels)}")
        if details.review_decision:
            facts.append(f"review decision: {details.review_decision}")
        if details.base:
            facts.append(f"into {details.base}")
    if pr.created_at:
        facts.append(f"opened {pr.created_at[:10]}")
    lines.append("; ".join(facts))
    if details and details.files:
        lines.append("Files:")
        lines += [f"  {f.path} +{f.additions} -{f.deletions}" for f in details.files[:_MAX_FILES]]
        if len(details.files) > _MAX_FILES:
            lines.append(f"  ... and {len(details.files) - _MAX_FILES} more")
    if details and details.body.strip():
        body = details.body.strip()
        lines.append("Description:")
        lines.append(body[:_MAX_BODY] + (" [...]" if len(body) > _MAX_BODY else ""))
    if item.previous and item.previous.summary:
        found = f" ({item.outcome.text()})" if item.outcome else ""
        lines.append(f"Reviewed before{found}: {item.previous.summary}")
    if item.rereview:
        at = (
            f" (which looked at {item.previous.head_sha[:7]})"
            if item.previous and item.previous.head_sha
            else ""
        )
        lines.append(f"Re-review: the author pushed new commits after the last review{at}.")
        if item.new_commits is None:
            lines.append("(Couldn't read the new commits.)")
        else:
            lines.append("New commits:")
            lines += [f"  - {subject}" for subject in item.new_commits[:_MAX_FILES]]
            if item.new_files:
                lines.append("Files they change:")
                lines += [f"  {change}" for change in item.new_files[:_MAX_FILES]]
                if len(item.new_files) > _MAX_FILES:
                    lines.append(f"  ... and {len(item.new_files) - _MAX_FILES} more")
    lines.append("")
    return lines


def plans_from(output: dict, refs: list[PRRef]) -> dict[PRRef, ReviewPlan]:
    """The lead's answer as plans, for the PRs asked about (others are ignored)."""
    wanted = {str(ref).lower(): ref for ref in refs}
    plans: dict[PRRef, ReviewPlan] = {}
    for item in output.get("plans") or []:
        if not isinstance(item, dict):
            continue
        ref = wanted.get(str(item.get("pr", "")).strip().lower())
        if ref is None or ref in plans:
            continue
        plan = ReviewPlan.from_dict({**item, "by": "lead"})
        plans[ref] = ReviewPlan(
            depth=plan.depth,
            priority=plan.priority,
            focus=plan.focus[:_MAX_FOCUS],
            why=plan.why,
            by="lead",
        )
    return plans


def default_plan(reason: str, *, requested: bool = False) -> ReviewPlan:
    """What a PR gets when the lead couldn't plan it: a full review."""
    priority = Priority.HIGH if requested else Priority.NORMAL
    return ReviewPlan(priority=priority, why=f"Not planned: {reason}")


class LeadAgent(Protocol):
    async def plan(self, notes: str) -> dict:
        """The plans for the PRs in ``notes``, as PLAN_SCHEMA describes."""
        ...


class ClaudeLead:
    """Plans reviews with a tool-less Agent SDK query and structured output.

    Args:
        model: Model to use; None uses Claude Code's configured default.
    """

    def __init__(self, *, model: str | None = None):
        self._model = model

    async def plan(self, notes: str) -> dict:
        options = ClaudeAgentOptions(
            system_prompt=INSTRUCTIONS,
            tools=[],
            setting_sources=[],
            output_format={"type": "json_schema", "schema": PLAN_SCHEMA},
            model=self._model,
            stderr=log_stderr,
        )
        result = None
        async for message in query(prompt=notes, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None or result.is_error or not isinstance(result.structured_output, dict):
            raise RuntimeError(getattr(result, "result", None) or "no plan came back")
        return result.structured_output
