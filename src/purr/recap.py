"""The "while you were away" recap: what's notable, and the agent that summarizes it."""

from datetime import datetime
from typing import Protocol

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from purr.claude import log_stderr
from purr.models import PR, Activity, PendingAction, PRRef, PRState

# Activity worth a recap. Pool PRs leaving (taken by someone, closed unreviewed)
# and review progress are noise.
_NOTABLE_CHANGES = {
    "assigned",
    "unassigned",
    "pooled",
    "new_commits",
    "closed",
    "merged",
    "requested",
    "replied",
}
_NOTABLE_KINDS = {
    "review.finished",
    "review.failed",
    "action.pending",
    "action.resolved",
    "session.permission_request",
}

INSTRUCTIONS = """\
You write the "While you were away" recap in Purr, an app for reviewing GitHub
pull requests with Claude. You get what happened while the user was away
(oldest first) and the current state of each PR involved.

Write:
- headline: one line with the counts that matter, what needs the user first.
  For example: "3 PRs need you, 1 review failed, 2 new PRs in the pool."
- items: one per PR, most urgent first: PRs waiting on the user (a pending
  action to approve, an agent waiting for permission, a failed review), then
  finished reviews, then new or changed PRs. Each text is a short phrase, like
  "Review done: wants to request changes (race on shutdown)". Set needs_you when
  the user has to decide something.

Only use what's in the notes. Don't invent findings.
"""

RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pr": {"type": "string", "description": "owner/repo#123"},
                    "text": {"type": "string"},
                    "needs_you": {"type": "boolean"},
                },
                "required": ["pr", "text", "needs_you"],
            },
        },
    },
    "required": ["headline", "items"],
}


def is_notable(activity: Activity) -> bool:
    if activity.kind == "pr.changed":
        return activity.data.get("change") in _NOTABLE_CHANGES
    if activity.kind == "session.permission_resolved":
        return not activity.data.get("answered", True)  # nobody was there to answer
    return activity.kind in _NOTABLE_KINDS


def event_text(activity: Activity) -> str:
    data = activity.data
    match activity.kind:
        case "pr.changed":
            return {
                "assigned": "assigned to the user",
                "unassigned": "no longer assigned to the user",
                "pooled": "new PR in the pool",
                "new_commits": "new commits pushed",
                "closed": "closed",
                "merged": "merged",
                "requested": "asks for the user's review",
                "replied": f"{data.get('by') or 'someone'} replied to the user",
            }.get(data.get("change", ""), str(data.get("change")))
        case "review.finished":
            findings = "; ".join(
                f"{f.get('severity')}: {f.get('text')}" for f in data.get("findings", [])
            )
            return f"review finished: {data.get('summary', '')}" + (
                f" Findings: {findings}" if findings else ""
            )
        case "review.failed":
            return f"review failed: {data.get('error', '')}"
        case "action.pending":
            return f"agent proposes: {data.get('description', '')}"
        case "action.resolved":
            return (
                "user approved the proposed action"
                if data.get("approved")
                else "proposed action dismissed"
            )
        case "session.permission_request":
            return f"agent waiting for permission to: {data.get('description', '')}"
        case "session.permission_resolved":
            return f"nobody answered about: {data.get('description', '')}; agent carried on"
    return activity.kind


def recap_notes(
    activities: list[Activity], prs: dict[PRRef, PR], pending: dict[PRRef, list[PendingAction]]
) -> str:
    """The recap agent's input: the events, then where each PR stands now."""
    lines = ["What happened:"]
    for activity in activities:
        when = datetime.fromisoformat(activity.at).strftime("%H:%M")
        pr = prs.get(activity.pr) if activity.pr else None
        title = f' ("{pr.title}")' if pr else ""
        lines.append(f"- {when} {activity.pr or ''}{title}: {event_text(activity)}")
    lines.append("\nWhere each PR stands now:")
    for ref, pr in prs.items():
        state = {
            PRState.NOT_REVIEWED: "not reviewed",
            PRState.REVIEWING: "being reviewed",
            PRState.NEEDS_YOU: "needs the user",
            PRState.DONE: "done",
        }[pr.state]
        actions = "; ".join(a.description for a in pending.get(ref, []))
        lines.append(
            f'- {ref} "{pr.title}": {state}, {pr.gh_state.value}'
            + (f", pending: {actions}" if actions else "")
        )
    return "\n".join(lines)


def fallback_recap(activities: list[Activity]) -> tuple[str, list[dict]]:
    """A plain recap for when the agent can't write one: the latest event per PR."""
    latest: dict[PRRef, Activity] = {}
    for activity in activities:
        if activity.pr:
            latest[activity.pr] = activity
    items = [
        {
            "pr": str(ref),
            "text": event_text(activity).capitalize(),
            "needs_you": activity.kind
            in {"action.pending", "review.failed", "session.permission_request"},
        }
        for ref, activity in reversed(latest.items())
    ]
    items.sort(key=lambda item: not item["needs_you"])
    plural = "s" if len(activities) != 1 else ""
    return f"{len(activities)} thing{plural} happened while you were away.", items


class RecapAgent(Protocol):
    async def summarize(self, notes: str) -> tuple[str, list[dict]]:
        """Returns ``(headline, items)``."""
        ...


class ClaudeRecapAgent:
    """Writes recaps with a tool-less Agent SDK query and structured output.

    Args:
        model: Model to use; None uses Claude Code's configured default.
    """

    def __init__(self, *, model: str | None = None):
        self._model = model

    async def summarize(self, notes: str) -> tuple[str, list[dict]]:
        options = ClaudeAgentOptions(
            system_prompt=INSTRUCTIONS,
            tools=[],
            setting_sources=[],
            output_format={"type": "json_schema", "schema": RECAP_SCHEMA},
            model=self._model,
            stderr=log_stderr,
        )
        result = None
        async for message in query(prompt=notes, options=options):
            if isinstance(message, ResultMessage):
                result = message
        if result is None or result.is_error or not isinstance(result.structured_output, dict):
            raise RuntimeError(getattr(result, "result", None) or "no recap came back")
        output = result.structured_output
        return str(output.get("headline", "")), list(output.get("items", []))
