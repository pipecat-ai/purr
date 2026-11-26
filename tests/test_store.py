import asyncio

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
    Reply,
    ReviewStatus,
    Severity,
    Verdict,
)
from purr.store import MIGRATIONS, Store
from tests.fakes import give_verdict


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

    # When it got to its state: moves with the state, not with GitHub's news.
    assert pr.state_at is None and updated.state_at
    await store.set_pr_state(pr.ref, PRState.NEEDS_YOU)
    assert (await store.get_pr(pr.ref)).state_at == updated.state_at
    await asyncio.sleep(0.005)  # timestamps are to the millisecond
    await store.set_pr_state(pr.ref, PRState.DONE)
    assert (await store.get_pr(pr.ref)).state_at > updated.state_at


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


async def test_where_a_pr_rests(store):
    """Needs you until your verdict on it as it is; your own PR, until nothing waits."""
    pr = await store.upsert_pr(make_pr(1))
    ref = pr.ref
    assert await store.resting_state(ref) == PRState.NOT_REVIEWED
    review = await store.start_review(ref)
    await store.fail_review(review.id, error="boom")
    assert await store.resting_state(ref) == PRState.NEEDS_YOU
    review = await store.start_review(ref)
    await store.finish_review(review.id, summary="Fine", findings=[])
    assert await store.resting_state(ref) == PRState.NEEDS_YOU  # reviewed, no verdict

    async def posted(kind: ActionKind, head: str = "abc123") -> None:
        action = await store.add_pending_action(ref, kind=kind, description=kind.value)
        await store.transition_action(
            action.id, expected=ActionStatus.PENDING, new=ActionStatus.EXECUTED, posted_head=head
        )

    await posted(ActionKind.COMMENT)
    assert await store.resting_state(ref) == PRState.NEEDS_YOU
    await posted(ActionKind.REQUEST_CHANGES, head="older")  # on an earlier commit
    assert await store.resting_state(ref) == PRState.NEEDS_YOU
    await posted(ActionKind.REQUEST_CHANGES)
    assert await store.decided(ref)
    assert await store.resting_state(ref) == PRState.DONE
    await posted(ActionKind.COMMENT)  # said something more: the verdict stands
    assert await store.resting_state(ref) == PRState.DONE
    await store.set_replies(ref, [Reply("alice", "Done?", "2026-11-21T10:00:00Z")])
    assert await store.resting_state(ref) == PRState.NEEDS_YOU

    # Given on GitHub, as it is (read with the replies): it counts all the same.
    other = await store.upsert_pr(make_pr(3))
    review = await store.start_review(other.ref)
    await store.finish_review(review.id, summary="Fine", findings=[])
    await store.set_verdict(other.ref, Verdict(ActionKind.APPROVE, "older"))
    assert await store.resting_state(other.ref) == PRState.NEEDS_YOU
    await store.set_verdict(other.ref, Verdict(ActionKind.APPROVE, other.head_sha))
    assert await store.resting_state(other.ref) == PRState.DONE

    mine = await store.upsert_pr(make_pr(2, authored=True))  # no verdict on your own
    assert await store.resting_state(mine.ref) == PRState.NOT_REVIEWED
    review = await store.start_review(mine.ref)
    await store.finish_review(review.id, summary="Answered", findings=[])
    assert await store.resting_state(mine.ref) == PRState.DONE


async def test_your_latest_verdict(store):
    """Posted through Purr or read from GitHub, the latest one is yours: whether you approved
    the PR (m can merge it) and whether it's done as it is."""
    ref = (await store.upsert_pr(make_pr(1))).ref
    assert await store.approved_head(ref) is None
    await give_verdict(store, ref, head="abc123")
    assert (await store.get_pr(ref)).verdict == Verdict(ActionKind.APPROVE, "abc123")
    assert await store.approved_head(ref) == "abc123"
    comment = await store.add_pending_action(ref, kind=ActionKind.COMMENT, description="c")
    await store.transition_action(
        comment.id, expected=ActionStatus.PENDING, new=ActionStatus.EXECUTED, posted_head="abc123"
    )
    assert await store.approved_head(ref) == "abc123"  # a comment isn't a verdict
    await store.set_verdict(ref, Verdict(ActionKind.REQUEST_CHANGES, "abc123"))  # on GitHub
    assert await store.approved_head(ref) is None
    await store.set_verdict(ref, None)  # dismissed
    assert (await store.get_pr(ref)).verdict is None

    # Done with none: done by the rule before, it needs one. With one, it stays done.
    await store.set_pr_state(ref, PRState.DONE)
    assert await store.done_without_verdict() == [ref]
    await store.set_verdict(ref, Verdict(ActionKind.APPROVE, "abc123"))
    assert await store.done_without_verdict() == []

    # What GitHub said before Purr posted one is older than it.
    await give_verdict(store, ref, head="abc123")
    await store.set_verdict(ref, None, read_at="2026-01-01T00:00:00+00:00")
    assert await store.approved_head(ref) == "abc123"


async def test_what_waits_on_you_in_a_repo(store):
    proposing, drafting, failing = [await store.upsert_pr(make_pr(n)) for n in (1, 2, 3)]
    other = await store.upsert_pr(PR("x/y", 4, "t", "a", "u", "s", "2026-11-20T10:00:00Z"))
    await store.add_pending_action(proposing.ref, kind=ActionKind.APPROVE, description="a")
    await store.add_pending_action(proposing.ref, kind=ActionKind.COMMENT, description="b")
    await store.add_pending_action(other.ref, kind=ActionKind.COMMENT, description="c")
    await store.save_comment(
        drafting.ref, path="a.py", line=1, side="RIGHT", body="Why?", head_sha="abc123"
    )
    review = await store.start_review(failing.ref)
    await store.fail_review(review.id, error="boom")
    repo = "pipecat-ai/pipecat"
    assert await store.proposed(repo) == {proposing.ref: ActionKind.APPROVE}  # the oldest
    assert await store.drafted(repo) == {drafting.ref}
    assert await store.failed(repo) == {failing.ref}


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


async def test_actions_survive_the_migration_that_allows_pushes(tmp_path):
    path = tmp_path / "purr.db"
    before = len(MIGRATIONS) - 2  # the one before pushes
    async with aiosqlite.connect(path) as db:
        script = "\n".join(MIGRATIONS[:before])
        await db.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {before};\nCOMMIT;")
        await db.execute(
            "INSERT INTO prs (repo, number, title, author, url, head_sha, updated_at)"
            " VALUES ('a/b', 1, 't', 'x', 'u', 's', '2026-11-20T10:00:00Z')"
        )
        await db.execute(
            "INSERT INTO pending_actions"
            " (id, pr_id, kind, description, payload, status, created_at, resolved_at, posted_head)"
            " VALUES ('x1', 1, 'approve', 'Approve', '{}', 'executed', 't', 't', 's')"
        )
        await db.commit()

    async with Store.connect(path) as store:
        assert await store.schema_version() == len(MIGRATIONS)
        ref = PRRef("a/b", 1)
        # Your verdicts start from what Purr posted.
        assert (await store.get_pr(ref)).verdict == Verdict(ActionKind.APPROVE, "s")
        assert await store.approved_head(ref) == "s"
        push = await store.add_pending_action(ref, kind=ActionKind.PUSH, description="Push")
        assert [a.kind for a in await store.pending_actions(ref)] == [push.kind]


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

    approved = await store.upsert_pr(make_pr(4))  # approved as it is: to merge
    await give_verdict(store, approved.ref, head=approved.head_sha)
    await store.set_pr_state(approved.ref, PRState.DONE)

    summaries = await store.repo_summaries()
    assert [(s.name, s.watched, s.needs_you, s.to_merge, s.not_reviewed) for s in summaries] == [
        ("daily-co/daily-x", False, 0, 0, 1),
        ("pipecat-ai/pipecat", True, 1, 1, 1),
        ("pipecat-ai/whisker", True, 0, 0, 0),
    ]
    assert summaries[1].last_activity is not None
    assert summaries[2].last_activity is None
    assert [
        pr.number for pr in await store.list_prs(repo="pipecat-ai/pipecat", open_only=True)
    ] == [
        2,
        1,
        4,
    ]


async def test_assignees_and_the_list_order(store):
    await store.upsert_pr(make_pr(1, created_at="2026-11-01T00:00:00Z"))
    await store.upsert_pr(make_pr(2, created_at="2026-11-10T00:00:00Z", assignees=("alice",)))
    await store.upsert_pr(make_pr(3, created_at="2026-11-05T00:00:00Z", assigned_to_me=True))

    # Newest first; who's assigned comes back as it went in.
    prs = await store.list_prs()
    assert [pr.number for pr in prs] == [2, 3, 1]
    assert prs[0].assignees == ("alice",) and prs[0].others_on_it

    # The repo list counts what's yours to review, not what others took.
    await store.add_repo("pipecat-ai/pipecat")
    [summary] = await store.repo_summaries()
    assert summary.not_reviewed == 2


async def test_prompt_history(store):
    a, b = PRRef("a/b", 1), PRRef("a/b", 2)
    await store.add_prompt(a, "is it safe?")
    await store.add_prompt(b, "tests?")
    await store.add_prompt(a, "and the docs?")
    assert await store.prompts(a) == [(a, "and the docs?"), (a, "is it safe?")]
    assert [text for _, text in await store.prompts()] == ["and the docs?", "tests?", "is it safe?"]


async def test_whats_new_since_you_looked(store):
    one, two, three = (PRRef("pipecat-ai/pipecat", n) for n in (1, 2, 3))
    for ref in (one, two, three):
        await store.upsert_pr(make_pr(ref.number))
    await store.record_activity("review.finished", pr=one, data={"summary": "ok"})
    await store.record_activity("pr.changed", pr=two, data={"change": "pooled"})  # not news
    await store.record_activity("pr.changed", pr=two, data={"change": "assigned"})
    await store.record_activity("review.started", pr=three)  # not news either
    await store.record_activity("session.permission_request", pr=one, data={"tool": "Bash"})
    # One per PR, at its latest news; the oldest news first.
    assert await store.unseen() == [(two, "assigned to you"), (one, "needs your OK")]

    await store.mark_seen(one)
    assert await store.unseen() == [(two, "assigned to you")]
    # Closed PRs aren't news.
    await store.apply_sync(gh_states={two: GHState.CLOSED})
    assert await store.unseen() == []


async def test_what_reviews_found_and_how_they_were_planned(store):
    from purr.models import Depth, Outcome, Priority, ReviewPlan

    one, two, three = (PRRef("pipecat-ai/pipecat", n) for n in (1, 2, 3))
    for ref in (one, two, three):
        await store.upsert_pr(make_pr(ref.number))
    plan = ReviewPlan(Depth.QUICK, Priority.HIGH, ("docs",), "Docs only", by="lead")
    review = await store.start_review(one, plan=plan)
    assert (await store.latest_review(one)).plan == plan
    await store.finish_review(
        review.id,
        summary="Two things",
        findings=[Finding(Severity.ISSUE, "Race"), Finding(Severity.NIT, "Name")],
    )
    clean = await store.start_review(two)
    await store.finish_review(clean.id, summary="Fine", findings=[])
    await store.start_review(three)  # still running: nothing to say yet

    assert await store.outcomes("pipecat-ai/pipecat") == {
        one: Outcome(issues=1, nits=1),
        two: Outcome(),
    }
    assert (await store.outcomes("other/repo")) == {}

    # News says it too.
    issue, nit = {"severity": "issue"}, {"severity": "nit"}
    await store.record_activity("review.finished", pr=one, data={"findings": [issue, issue]})
    await store.record_activity("review.finished", pr=two, data={"findings": [nit]})
    await store.record_activity("review.finished", pr=three, data={"findings": [issue, nit]})
    assert await store.unseen() == [
        (one, "review: found 2 issues"),
        (two, "review: nothing found"),  # nits don't count
        (three, "review: found 1 issue"),
    ]


async def test_replies_to_you(store):
    ref = PRRef("pipecat-ai/pipecat", 1)
    await store.upsert_pr(make_pr(1))
    assert (await store.get_pr(ref)).replies == ()
    assert not await store.waits_on_you(ref)

    alice = Reply("alice", "Fixed.", "2026-11-22T10:00:00Z", "u1", "bus.py:42")
    bob = Reply("bob", "Why?", "2026-11-22T11:00:00Z")
    await store.set_replies(ref, [alice])
    assert (await store.get_pr(ref)).replies == (alice,)
    assert await store.waits_on_you(ref)
    # GitHub's metadata changing leaves them.
    await store.upsert_pr(make_pr(1, title="Renamed"))
    assert (await store.get_pr(ref)).replies == (alice,)

    # x: they need no answer, the ones seen so far; a later one does.
    await store.dismiss_replies(ref)
    assert (await store.get_pr(ref)).replies == ()
    assert not await store.waits_on_you(ref)
    await store.set_replies(ref, [alice, bob])
    assert (await store.get_pr(ref)).replies == (bob,)
    # You answered on GitHub: none since your last word.
    await store.set_replies(ref, [])
    assert (await store.get_pr(ref)).replies == ()


async def test_review_requests_and_runs(store):
    ref = PRRef("pipecat-ai/pipecat", 1)
    await store.upsert_pr(make_pr(1, requested_from_me=True, assignees=("alice",)))
    assert (await store.get_pr(ref)).requested_from_me
    # Asked of you, though someone else has it: still one for you to look at.
    [summary] = await store.repo_summaries()
    assert summary.not_reviewed == 1

    assert not await store.running(ref)
    run = await store.start_run(ref, kind="review", review_id=None, session_id=None)
    assert await store.running(ref)
    await store.finish_run(run)
    assert not await store.running(ref)
