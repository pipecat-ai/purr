"""The "while you were away" recap: what's notable, and the agent that summarizes it."""

from datetime import datetime
from typing import Protocol

from purr.claude import structured
from purr.models import PR, Activity, PendingAction, PRRef, PRState
from purr.news import recap_text

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


def recap_notes(
    activities: list[Activity], prs: dict[PRRef, PR], pending: dict[PRRef, list[PendingAction]]
) -> str:
    """The recap agent's input: the events, then where each PR stands now."""
    lines = ["What happened:"]
    for activity in activities:
        when = datetime.fromisoformat(activity.at).strftime("%H:%M")
        pr = prs.get(activity.pr) if activity.pr else None
        title = f' ("{pr.title}")' if pr else ""
        lines.append(f"- {when} {activity.pr or ''}{title}: {recap_text(activity)}")
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
            "text": recap_text(activity).capitalize(),
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
        output = await structured(
            notes, instructions=INSTRUCTIONS, schema=RECAP_SCHEMA, model=self._model, what="recap"
        )
        return str(output.get("headline", "")), list(output.get("items", []))
