import pytest

from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Finding,
    PRRef,
    PRState,
    ReviewStatus,
    Severity,
)
from purr.store import MIGRATIONS, Store


def make_pr(number: int, *, updated_at: str = "2026-11-20T10:00:00+00:00", **kwargs) -> PR:
    return PR(
        repo="pipecat-ai/pipecat",
        number=number,
        title=kwargs.pop("title", f"PR {number}"),
        author="someone",
        url=f"https://github.com/pipecat-ai/pipecat/pull/{number}",
        head_sha="abc123",
        updated_at=updated_at,
        **kwargs,
    )


@pytest.fixture
async def store():
    async with Store.connect(":memory:") as store:
        yield store


async def test_migrations_apply_once(tmp_path):
    path = tmp_path / "purr.db"
    async with Store.connect(path) as store:
        assert await store.schema_version() == len(MIGRATIONS)
        await store.upsert_pr(make_pr(1))
    async with Store.connect(path) as store:
        assert await store.schema_version() == len(MIGRATIONS)
        assert await store.get_pr(PRRef("pipecat-ai/pipecat", 1)) is not None


async def test_upsert_refreshes_metadata_but_keeps_state(store):
    pr = await store.upsert_pr(make_pr(1))
    assert pr.id is not None
    assert pr.state == PRState.NOT_REVIEWED

    await store.set_pr_state(pr.ref, PRState.NEEDS_YOU)
    updated = await store.upsert_pr(make_pr(1, title="Renamed", assigned_to_me=True))

    assert updated.id == pr.id
    assert updated.title == "Renamed"
    assert updated.assigned_to_me is True
    assert updated.state == PRState.NEEDS_YOU


async def test_list_prs_puts_needs_you_first(store):
    for number, state in [
        (1, PRState.DONE),
        (2, PRState.NOT_REVIEWED),
        (3, PRState.REVIEWING),
        (4, PRState.NEEDS_YOU),
    ]:
        pr = await store.upsert_pr(make_pr(number))
        await store.set_pr_state(pr.ref, state)
    await store.upsert_pr(make_pr(5, updated_at="2026-11-20T11:00:00+00:00"))

    assert [pr.number for pr in await store.list_prs()] == [4, 3, 5, 2, 1]
    assert [pr.number for pr in await store.list_prs(PRState.NOT_REVIEWED)] == [5, 2]


async def test_set_state_of_unknown_pr_raises(store):
    with pytest.raises(KeyError):
        await store.set_pr_state(PRRef("a/b", 1), PRState.DONE)


async def test_review_lifecycle(store):
    pr = await store.upsert_pr(make_pr(1))
    review = await store.start_review(pr.ref, worktree_path="/tmp/wt")
    assert review.status == ReviewStatus.RUNNING
    assert review.pr == pr.ref

    await store.set_review_session(review.id, "session-123")
    assert [r.id for r in await store.interrupted_reviews()] == [review.id]

    finished = await store.finish_review(
        review.id,
        summary="Looks good",
        findings=[
            Finding(severity=Severity.ISSUE, text="Race on shutdown", file="bus.py", line=42),
            Finding(severity=Severity.NIT, text="Unused import"),
        ],
    )
    assert finished.status == ReviewStatus.FINISHED
    assert finished.session_id == "session-123"
    assert finished.finished_at is not None
    assert await store.interrupted_reviews() == []

    findings = await store.findings(review.id)
    assert [(f.severity, f.file, f.line) for f in findings] == [
        (Severity.ISSUE, "bus.py", 42),
        (Severity.NIT, None, None),
    ]
    latest = await store.latest_review(pr.ref)
    assert latest is not None and latest.id == review.id


async def test_failed_review(store):
    pr = await store.upsert_pr(make_pr(1))
    review = await store.start_review(pr.ref)
    failed = await store.fail_review(review.id, error="boom")
    assert failed.status == ReviewStatus.FAILED
    assert failed.error == "boom"


async def test_pending_action_can_only_be_resolved_once(store):
    pr = await store.upsert_pr(make_pr(1))
    action = await store.add_pending_action(
        pr.ref,
        kind=ActionKind.REQUEST_CHANGES,
        description="Post 3 comments + request changes",
        payload={"comments": [{"file": "bus.py", "line": 42, "body": "Race here"}]},
    )
    assert action.status == ActionStatus.PENDING
    assert action.payload["comments"][0]["line"] == 42
    assert [a.id for a in await store.pending_actions(pr.ref)] == [action.id]

    approved = await store.transition_action(
        action.id, expected=ActionStatus.PENDING, new=ActionStatus.APPROVED
    )
    assert approved is not None and approved.status == ActionStatus.APPROVED
    assert approved.resolved_at is not None

    # A second decision (e.g. a dismiss by voice racing the TUI approval) loses.
    assert (
        await store.transition_action(
            action.id, expected=ActionStatus.PENDING, new=ActionStatus.DISMISSED
        )
        is None
    )
    assert await store.pending_actions() == []
