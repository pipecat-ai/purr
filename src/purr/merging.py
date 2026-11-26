"""Deciding to merge: what stops it outright, and what's your call.

``m`` on a PR you approved shows where it stands, fresh from GitHub; ``m`` again
merges. Approving and merging are apart on purpose: someone else may be
reviewing it too, and you may want them to approve as well.

- **Blockers** stop a merge: you haven't approved it (on GitHub or here), it has
  commits since you did (approve again), it has conflicts, it's a draft.
- **Concerns** are for you to weigh: checks that failed or are still running (a
  docs build failing needn't hold it up), assignees who haven't approved, a
  branch rule GitHub says isn't met (it may refuse; it says why), GitHub still
  working out whether it can be merged (it does, in seconds, once asked).
"""

from purr.models import CheckState, MergeCheck


def blockers(check: MergeCheck, approved_head: str | None) -> list[str]:
    """What stops the merge outright; there's no deciding past these."""
    found = []
    if not approved_head:
        found.append("You haven't approved it")
    elif check.head_sha != approved_head:
        found.append("It has new commits since you approved it: approve again first")
    if check.mergeable == "CONFLICTING" or check.merge_state == "DIRTY":
        found.append(f"It has conflicts with {check.base}")
    if check.merge_state == "DRAFT":
        found.append("It's a draft")
    return found


def concerns(check: MergeCheck, me: str) -> list[str]:
    """What you may want to wait for; merging anyway is your call."""
    return [
        *check_concerns(check),
        *_review_concerns(check, me),
        *_rule_concerns(check),
        *([WORKING_OUT] if working_out(check) else []),
    ]


def check_concerns(check: MergeCheck) -> list[str]:
    """Checks that failed or are still running."""
    found = []
    for state, what in ((CheckState.FAILED, "failed"), (CheckState.RUNNING, "is still running")):
        for run in (run for run in check.checks if run.state == state):
            required = " (required)" if run.required else ""
            found.append(f"{run.name}{required} {what}")
    return found


def _review_concerns(check: MergeCheck, me: str) -> list[str]:
    """Other assignees who haven't approved: they may want to."""
    reviews = dict(check.reviews)
    others = [login for login in check.assignees if login.lower() != me.lower()]
    return [
        f"{login} {_said(reviews.get(login, ''))}"
        for login in others
        if reviews.get(login) != "APPROVED"
    ]


def _said(state: str) -> str:
    return _SAID.get(state, "hasn't approved")


_SAID = {"CHANGES_REQUESTED": "requested changes"}


_RULES = {
    "BLOCKED": "A branch rule isn't met: GitHub may refuse",
    "BEHIND": "It's behind {base}: GitHub may want its branch updated first",
}

WORKING_OUT = "GitHub is still working out whether it can be merged"


def _rule_concerns(check: MergeCheck) -> list[str]:
    rule = _RULES.get(check.merge_state or "")
    return [rule.format(base=check.base)] if rule else []


def working_out(check: MergeCheck) -> bool:
    """GitHub hasn't worked out yet whether it can be merged: it starts when asked, and
    takes seconds."""
    return "UNKNOWN" in (check.mergeable, check.merge_state)


def new_concerns(fresh: list[str], seen: list[str]) -> list[str]:
    """Concerns now that weren't there when you decided (things that only got better don't
    count)."""
    return [concern for concern in fresh if concern not in seen]
