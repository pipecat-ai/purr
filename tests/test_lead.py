"""What the lead is told about PRs, and how its answer becomes plans."""

import asyncio

from purr.lead import LeadInput, RepoContext, default_plan, lead_notes, plans_from
from purr.models import (
    Depth,
    FileChange,
    Outcome,
    PRDetails,
    Priority,
    PRRef,
    Review,
    ReviewPlan,
    ReviewStatus,
)
from purr.review.skills import RepoSkill
from purr.workers.slots import Slots
from tests.test_reviews import make_pr


def test_lead_notes_say_what_matters_per_repo_and_pr():
    pr = make_pr(assigned_to_me=True)
    details = PRDetails(
        body="Adds a bus.\n" + "x" * 3000,
        base="main",
        additions=420,
        deletions=12,
        changed_files=40,
        files=tuple(FileChange(f"src/f{i}.py", 10, 1) for i in range(40)),
        labels=("release",),
        checks="FAILURE",
    )
    previous = Review(
        id=1, pr=pr.ref, status=ReviewStatus.FINISHED, started_at="", summary="One race."
    )
    context = RepoContext(
        skills=[
            RepoSkill("pipecat:code-review", "Code review", model_invocable=False),
            RepoSkill("pipecat:prose-review", "Check the prose"),
        ],
        review_skill="pipecat:code-review",
        instructions="Always look at thread safety.",
    )
    notes = lead_notes(
        [LeadInput(pr, details, previous, Outcome(issues=1), requested=True)],
        {pr.repo: context},
    )
    assert "Full reviews here follow the repo's /pipecat:code-review skill." in notes
    assert "- pipecat:prose-review: Check the prose" in notes
    assert "- pipecat:code-review" not in notes  # the agent couldn't load it
    assert "Always look at thread safety." in notes
    assert '## pipecat-ai/pipecat#2841 "Add message bus" by someone' in notes
    assert "the user asked for this review now; assigned to the user" in notes
    assert "+420 -12 in 40 files; CI: FAILURE; labels: release" in notes
    assert "src/f29.py +10 -1" in notes and "src/f30.py" not in notes
    assert "... and 10 more" in notes
    assert details.body[:1500] + " [...]" in notes and details.body[:1501] not in notes
    assert "Reviewed before (found 1 issue): One race." in notes


def test_lead_notes_for_a_rereview():
    pr = make_pr(assigned_to_me=True)
    previous = Review(
        id=1, pr=pr.ref, status=ReviewStatus.FINISHED, started_at="", summary="One race.",
        head_sha="abc1234567",
    )  # fmt: skip
    item = LeadInput(
        pr, previous=previous, rereview=True, new_commits=["Fix the race"], new_files=["M a.py"]
    )
    notes = lead_notes([item], {})
    assert "after the last review (which looked at abc1234)." in notes
    assert "New commits:\n  - Fix the race\nFiles they change:\n  M a.py" in notes
    unknown = LeadInput(pr, previous=previous, rereview=True)
    assert "(Couldn't read the new commits.)" in lead_notes([unknown], {})


def test_plans_from_the_leads_answer():
    a, b, c = PRRef("o/r", 1), PRRef("o/r", 2), PRRef("o/r", 3)
    output = {
        "plans": [
            {"pr": "O/R#1", "depth": "quick", "priority": "high", "focus": ["a"] * 6, "why": "x"},
            {"pr": "o/r#1", "depth": "full", "priority": "low", "focus": [], "why": "dup"},
            {"pr": "o/r#2", "depth": "deep", "priority": "urgent", "focus": [], "why": "y"},
            {"pr": "o/r#9", "depth": "quick", "priority": "low", "focus": [], "why": "?"},
        ]
    }
    plans = plans_from(output, [a, b, c])
    assert plans[a] == ReviewPlan(Depth.QUICK, Priority.HIGH, ("a",) * 4, "x", by="lead")
    # Values it doesn't know take the defaults.
    assert (plans[b].depth, plans[b].priority, plans[b].by) == (Depth.FULL, Priority.NORMAL, "lead")
    assert c not in plans  # left out: the worker plans it by default


def test_default_plan_is_a_full_review():
    plan = default_plan("the lead couldn't plan it (offline)")
    assert (plan.depth, plan.priority, plan.by) == (Depth.FULL, Priority.NORMAL, "default")
    assert default_plan("x", requested=True).priority == Priority.HIGH
    assert ReviewPlan.from_dict(plan.to_dict()) == plan


def test_outcome():
    assert Outcome.of([]).text() == "nothing found"
    findings = [{"severity": "issue"}, {"severity": "nit"}, {"severity": "nit"}]
    assert Outcome.of(findings).text() == "found 1 issue, 2 nits"
    assert Outcome.of(findings).found
    # Only issues count: suggestions and nits are mentioned, but nothing was found.
    minor = Outcome.of([{"severity": "suggestion"}, {"severity": "nit"}, {"severity": "nit"}])
    assert not minor.found
    assert minor.text() == "nothing found (1 suggestion, 2 nits)"


async def test_slots_go_by_priority_then_arrival():
    slots = Slots(1)
    assert slots.join("first", 1).done()  # a free slot
    low = slots.join("low", 2)
    normal = slots.join("normal", 1)
    high = slots.join("high", 0)
    also_high = slots.join("also high", 0)
    assert slots.waiting() == ["high", "also high", "normal", "low"]

    slots.release()
    assert high.done() and not also_high.done()
    slots.leave(also_high)  # stopped waiting
    assert slots.waiting() == ["normal", "low"]
    slots.release()
    assert normal.done()

    # Handed a slot just as it stopped waiting: the slot goes to the next one.
    slots.release()
    assert low.done()
    slots.leave(low)
    assert slots.join("next", 1).done()
    await asyncio.sleep(0)
