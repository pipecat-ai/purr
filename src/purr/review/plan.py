"""How a review agent is told what the lead planned for its PR (see :mod:`purr.lead`)."""

from purr.models import Depth, ReviewPlan

_QUICK = """\
Keep it quick: read the diff and the code it touches, check the points below,
and finish. Don't start subagents or long investigations. If something does
look wrong, dig into that one thing, and say in your summary why it needed it."""

_FULL = """\
Be thorough. Where checks are independent (bugs, the repo's rules, tests,
docs), run them in parallel with subagents (the Agent tool), and check what
they report before passing it on: only real problems become findings."""


def plan_section(plan: ReviewPlan) -> str:
    """The plan as a system prompt section (none to answer comments: that's no review)."""
    if plan.answers_comments:
        return ""
    why = f" ({plan.why})" if plan.why else ""
    parts = [
        f"\n\n## How this review should go\n\nPurr's lead planned a {plan.depth} review{why}.",
        _QUICK if plan.depth == Depth.QUICK else _FULL,
    ]
    if plan.focus:
        parts.append("Look hardest at:\n" + "\n".join(f"- {point}" for point in plan.focus))
    parts.append(
        "Finding nothing is a fine outcome: then call submit_review with no findings, "
        "and say so plainly."
    )
    return "\n\n".join(parts)
