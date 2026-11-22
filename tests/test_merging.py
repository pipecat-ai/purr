"""What stops a merge, and what's your call."""

from dataclasses import replace

from purr.merging import blockers, concerns, new_concerns
from purr.models import Check, CheckState, MergeCheck

READY = MergeCheck(
    head_sha="abc",
    base="main",
    method="merge",
    mergeable="MERGEABLE",
    merge_state="CLEAN",
    checks=(Check("Unit Tests", CheckState.PASSED),),
    assignees=("me",),
    reviews=(("me", "APPROVED"),),
)


def test_ready():
    assert blockers(READY, "abc") == []
    assert concerns(READY, "me") == []


def test_what_stops_a_merge():
    assert blockers(READY, None) == ["You haven't approved it here"]
    assert blockers(READY, "older") == [
        "It has new commits since you approved it: approve again first"
    ]
    conflicts = replace(READY, mergeable="CONFLICTING", merge_state="DIRTY")
    assert blockers(conflicts, "abc") == ["It has conflicts with main"]
    assert blockers(replace(READY, merge_state="DRAFT"), "abc") == ["It's a draft"]


def test_what_you_decide_past():
    checks = (
        Check("Unit Tests", CheckState.PASSED),
        Check("docs/readthedocs", CheckState.FAILED),
        Check("Build", CheckState.FAILED, required=True),
        Check("Lint", CheckState.RUNNING),
        Check("Optional", CheckState.SKIPPED),
    )
    pr = replace(
        READY,
        checks=checks,
        merge_state="BLOCKED",
        assignees=("Me", "alice", "bob"),
        reviews=(("me", "APPROVED"), ("bob", "CHANGES_REQUESTED")),
    )
    assert concerns(pr, "me") == [
        "docs/readthedocs failed",
        "Build (required) failed",
        "Lint is still running",
        "alice hasn't approved",
        "bob requested changes",
        "GitHub says a branch rule isn't met (a required review or check): it may refuse",
    ]


def test_only_something_new_since_you_looked_counts():
    seen = ["Lint is still running", "docs/readthedocs failed"]
    assert new_concerns(["docs/readthedocs failed"], seen) == []  # Lint passed meanwhile
    assert new_concerns(["Lint failed", "docs/readthedocs failed"], seen) == ["Lint failed"]
