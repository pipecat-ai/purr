"""What you might ask next in a PR's session, once the agent's done: like Claude Code's
prompt suggestions, shown dim in the prompt for ``tab`` to take.

Claude Code makes these itself, but not for every account, and not through the Agent
SDK's messages, so Purr asks a small model, from the end of the conversation.
"""

from typing import Protocol

from purr.claude import structured
from purr.models import PR, PendingAction
from purr.review.tools import PROPOSE_ACTION, PURR_TOOL_PREFIX, SUBMIT_REVIEW

INSTRUCTIONS = """\
You suggest what the user will type next in Purr, an app where they review GitHub
pull requests with a Claude agent. The agent has reviewed a PR, and the user talks
with it to dig in and decide what to post. You get the PR and the end of their
conversation.

Write the one message the user is most likely to send next, as they'd type it: a
short line (at most 12 words), specific to this PR (name the finding, function or
file), that moves them toward a decision: dig into a finding, have the agent check
something it couldn't, or change what gets posted. Not thanks, not something the
conversation already answered, and not approving or dismissing the proposal (the
user has keys for that). If nothing stands out, return an empty suggestion.
"""

SCHEMA = {
    "type": "object",
    "properties": {"suggestion": {"type": "string"}},
    "required": ["suggestion"],
}

# How much of the conversation's end the model reads.
_TAIL = 12_000


def _submitted(entry: dict) -> str:
    review = entry.get("input") or {}
    findings = "; ".join(
        f"{f.get('severity')}: {f.get('text')}" for f in review.get("findings") or []
    )
    return f"Agent's review: {review.get('summary', '')} Findings: {findings or 'none'}"


def _proposed(entry: dict) -> str:
    proposal = entry.get("input") or {}
    comments = len(proposal.get("comments") or [])
    return f"Agent proposed: {proposal.get('description', '')} ({comments} inline comments)"


# What each kind of step says, for the model; the rest (tool calls, results,
# reasoning) is left out.
_TOOLS = {
    PURR_TOOL_PREFIX + SUBMIT_REVIEW: _submitted,
    PURR_TOOL_PREFIX + PROPOSE_ACTION: _proposed,
}
_SAID = {
    "user": lambda entry: f"User: {entry.get('text', '')}",
    "text": lambda entry: f"Agent: {entry.get('text', '')}",
}


def _line(entry: dict) -> str | None:
    if entry.get("kind") == "tool_use":
        told = _TOOLS.get(str(entry.get("name")))
    else:
        told = _SAID.get(str(entry.get("kind")))
    return told(entry) if told else None


def suggestion_notes(pr: PR, entries: list[dict], pending: list[PendingAction]) -> str:
    """What the model reads: the PR, what waits on the user, and the conversation's end."""
    lines = [line for entry in entries if (line := _line(entry))]
    conversation = "\n\n".join(lines)[-_TAIL:]
    waiting = "".join(f"\nWaiting for the user's OK: {a.description}" for a in pending)
    return (
        f'PR: {pr.repo}#{pr.number} "{pr.title}" by {pr.author}{waiting}\n\n'
        f"The conversation (its end):\n\n{conversation}"
    )


class Suggester(Protocol):
    async def suggest(self, notes: str) -> str:
        """The next message, or "" if nothing stands out."""
        ...


class ClaudeSuggester:
    """Suggests with a tool-less Agent SDK query and structured output.

    Args:
        model: Model to use (a small one: it's a line, after every turn you look at).
    """

    def __init__(self, *, model: str | None):
        self._model = model

    async def suggest(self, notes: str) -> str:
        output = await structured(
            notes, instructions=INSTRUCTIONS, schema=SCHEMA, model=self._model, what="suggestion"
        )
        return one_line(str(output.get("suggestion", "")))


def one_line(text: str) -> str:
    return " ".join(text.split())
