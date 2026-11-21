import aiosqlite
import pytest

from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Finding,
    GHState,
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


async def test_migrating_a_v1_database_keeps_prs(tmp_path):
    path = tmp_path / "purr.db"

    async with aiosqlite.connect(path) as db:
        await db.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
        await db.execute(
            "INSERT INTO prs (repo, number, title, author, url, head_sha, updated_at)"
            " VALUES ('a/b', 1, 't', 'x', 'u', 's', '2026-11-20T10:00:00Z')"
        )
        await db.commit()

    async with Store.connect(path) as store:
        assert await store.schema_version() == len(MIGRATIONS)
        pr = await store.get_pr(PRRef("a/b", 1))
        assert pr is not None and pr.gh_state == GHState.OPEN


async def test_repos_are_listed_alphabetically_ignoring_case(store):
    for name in ["pipecat-ai/whisker", "daily-co/daily-x", "Pipecat-ai/pipecat"]:
        await store.add_repo(name)
    await store.add_repo("daily-co/DAILY-X")  # already watched

    assert [r.name for r in await store.list_repos()] == [
        "daily-co/daily-x",
        "Pipecat-ai/pipecat",
        "pipecat-ai/whisker",
    ]
    assert (await store.get_repo("daily-co/daily-x")).synced_at is None
    await store.mark_synced(["daily-co/daily-x"])
    assert (await store.get_repo("daily-co/daily-x")).synced_at is not None


async def test_removing_a_repo_keeps_prs_that_matter(store):
    await store.add_repo("pipecat-ai/pipecat")
    pool = await store.upsert_pr(make_pr(1))
    assigned = await store.upsert_pr(make_pr(2, assigned_to_me=True))
    reviewed = await store.upsert_pr(make_pr(3))
    await store.set_pr_state(reviewed.ref, PRState.DONE)

    assert await store.remove_repo("pipecat-ai/pipecat") is True
    assert await store.remove_repo("pipecat-ai/pipecat") is False
    assert await store.list_repos() == []
    assert await store.get_pr(pool.ref) is None
    assert await store.get_pr(assigned.ref) is not None
    assert await store.get_pr(reviewed.ref) is not None


async def test_apply_sync(store):
    keep = await store.upsert_pr(make_pr(1))
    gone = await store.upsert_pr(make_pr(2))
    merged = await store.upsert_pr(make_pr(3))

    await store.apply_sync(
        upserts=[make_pr(1, title="Renamed"), make_pr(4)],
        deletes=[gone.ref],
        gh_states={merged.ref: GHState.MERGED},
    )

    assert (await store.get_pr(keep.ref)).title == "Renamed"
    assert await store.get_pr(gone.ref) is None
    assert (await store.get_pr(merged.ref)).gh_state == GHState.MERGED
    assert sorted(pr.number for pr in await store.open_prs()) == [1, 4]


async def test_activity_log(store):
    ref = PRRef("pipecat-ai/pipecat", 1)
    first = await store.record_activity("pr.changed", pr=ref, data={"change": "pooled"})
    await store.record_activity("something.global")

    entries = await store.activity()
    assert [(a.kind, a.pr) for a in entries] == [
        ("pr.changed", ref),
        ("something.global", None),
    ]
    assert entries[0].data == {"change": "pooled"}
    assert [a.id for a in await store.activity(pr=ref)] == [first.id]
    assert await store.activity(since="2999-01-01") == []


async def test_repo_summaries(store):
    await store.add_repo("pipecat-ai/pipecat")
    await store.add_repo("pipecat-ai/whisker")
    await store.upsert_pr(make_pr(1))
    needs = await store.upsert_pr(make_pr(2))
    await store.set_pr_state(needs.ref, PRState.NEEDS_YOU)
    closed = await store.upsert_pr(make_pr(3))
    await store.apply_sync(gh_states={closed.ref: GHState.CLOSED})
    elsewhere = make_pr(9, assigned_to_me=True)
    elsewhere.repo = "daily-co/daily-x"
    await store.upsert_pr(elsewhere)
    await store.record_activity("pr.changed", pr=needs.ref)

    summaries = await store.repo_summaries()
    assert [(s.name, s.watched, s.needs_you, s.not_reviewed) for s in summaries] == [
        ("daily-co/daily-x", False, 0, 1),
        ("pipecat-ai/pipecat", True, 1, 1),
        ("pipecat-ai/whisker", True, 0, 0),
    ]
    assert summaries[1].last_activity is not None
    assert summaries[2].last_activity is None
    assert [
        pr.number for pr in await store.list_prs(repo="pipecat-ai/pipecat", open_only=True)
    ] == [
        2,
        1,
    ]
