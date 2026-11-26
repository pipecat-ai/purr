"""Approving and dismissing pending actions."""

import asyncio
from dataclasses import replace

import pytest
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.actions import ActionError, execute
from purr.git import GitError
from purr.jobs import Busy, Done, Failed, Invalid, NotFound, request
from purr.messages import (
    ApproveAction,
    ApprovePR,
    AssignMe,
    CheckMerge,
    DeleteGitHubComment,
    DismissAction,
    DropProposedComment,
    EditGitHubComment,
    EditPRComment,
    EditProposedComment,
    Merged,
    MergePR,
    PostComments,
    PostLineComment,
    PostPRComment,
    RemoveComment,
    ReplyToThread,
    SaveComment,
)
from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Check,
    CheckState,
    GHState,
    MergeCheck,
    PendingAction,
    PRRef,
    PRState,
)
from purr.store import Store
from purr.workers import GitHubWorker
from tests.fakes import FakeGitHub, FakeRepos, FakeWriter

REF = PRRef("pipecat-ai/pipecat", 2841)
COMMENTS = [{"path": "bus.py", "line": 42, "body": "Race here"}]


def action(kind=ActionKind.REQUEST_CHANGES, **payload) -> PendingAction:
    return PendingAction(
        id="a1",
        pr=REF,
        kind=kind,
        description="Post 1 comment and request changes",
        created_at="2026-11-20T10:00:00+00:00",
        payload=payload,
    )


async def test_execute_posts_a_review_with_inline_comments():
    writer = FakeWriter()
    posted = await execute(writer, action(body="Two things.", comments=COMMENTS), head_sha="abc")
    [review] = writer.reviews
    assert review["event"] == "REQUEST_CHANGES" and review["commit_id"] == "abc"
    assert review["comments"] == [
        {"path": "bus.py", "line": 42, "side": "RIGHT", "body": "Race here"}
    ]
    assert posted.url.endswith("#review-1") and posted.warnings == []


async def test_replies_go_into_existing_threads():
    writer = FakeWriter()
    replies = [{"thread_id": "T1", "body": "Fixed now, thanks."}]
    await execute(writer, action(ActionKind.APPROVE, replies=replies), head_sha="abc")
    assert [r["event"] for r in writer.reviews] == ["APPROVE"]
    assert writer.replies == [("T1", "Fixed now, thanks.")]

    # Only replies: no empty review gets posted.
    writer = FakeWriter()
    posted = await execute(writer, action(ActionKind.COMMENT, replies=replies), head_sha="abc")
    assert writer.reviews == [] and writer.replies == [("T1", "Fixed now, thanks.")]
    assert posted.url == "https://github.com/reply/1"

    # A reply failing after the review is up is a warning, not an error.
    writer = FakeWriter()
    writer.fail_replies = True
    posted = await execute(writer, action(body="x", replies=replies), head_sha="abc")
    assert len(writer.reviews) == 1
    assert "didn't go through" in posted.warnings[0]


async def test_comments_github_cant_place_go_into_the_body():
    writer = FakeWriter()
    writer.fail_with = ["Unprocessable Entity: Line could not be resolved"]
    await execute(writer, action(body="Two things.", comments=COMMENTS), head_sha="abc")
    [review] = writer.reviews
    assert review["comments"] == []
    assert review["body"] == "Two things.\n\n- `bus.py:42`: Race here"


async def test_execute_details():
    writer = FakeWriter()
    await execute(writer, action(ActionKind.APPROVE), head_sha="abc")
    await execute(writer, action(ActionKind.COMMENT), head_sha="abc")
    assert [r["event"] for r in writer.reviews] == ["APPROVE", "COMMENT"]
    assert writer.reviews[0]["body"] == "LGTM"  # an approval with nothing else to say
    assert writer.reviews[1]["body"] == "Post 1 comment and request changes"
    with pytest.raises(ActionError, match="can't merge"):
        await execute(writer, action(ActionKind.MERGE), head_sha="abc")


@pytest.fixture
async def setup():
    async with Store.connect(":memory:") as store:
        # Seed before the worker starts, so its first poll sees a reviewed PR to keep.
        await store.upsert_pr(
            PR(
                repo=REF.repo,
                number=REF.number,
                title="Add message bus",
                author="someone",
                url="u",
                head_sha="abc",
                updated_at="2026-11-20T10:00:00Z",
            )
        )
        await store.set_pr_state(REF, PRState.NEEDS_YOU)
        pending = await store.add_pending_action(
            REF,
            kind=ActionKind.REQUEST_CHANGES,
            description="Post 1 comment",
            payload={"comments": COMMENTS},
        )
        writer = FakeWriter()
        worker = GitHubWorker(store=store, github=FakeGitHub(), writer=writer, interval=3600)
        client = BaseWorker("client")
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            yield store, writer, client, pending
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def test_approving_posts_once_and_the_pr_is_done(setup):
    store, writer, client, pending = setup
    posted = await request(client, ApproveAction(action_id=pending.id), timeout=5)
    assert posted.url.endswith("#review-1")
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.EXECUTED
    assert (await store.get_pr(REF)).state == PRState.DONE

    with pytest.raises(NotFound, match="already decided"):
        await request(client, ApproveAction(action_id=pending.id), timeout=5)
    assert len(writer.reviews) == 1


def mine(line: int, body: str, *, side="RIGHT", head="abc", comment_id=None) -> SaveComment:
    return SaveComment(
        pr=REF, path="src/bus.py", line=line, side=side, body=body, head_sha=head,
        comment_id=comment_id,
    )  # fmt: skip


async def test_your_comments_go_with_the_review_you_approve(setup):
    store, writer, client, pending = setup
    await request(client, mine(10, "Why a lock here?"), timeout=5)
    await request(client, mine(7, "Keep this check", side="LEFT"), timeout=5)
    await request(client, mine(3, "Fixed since", head="old"), timeout=5)  # an earlier commit
    with pytest.raises(Invalid):
        await request(client, mine(4, "  "), timeout=5)
    first, second, older = await store.draft_comments(REF)
    await request(client, mine(10, "Why a lock?", comment_id=first.id), timeout=5)  # edited
    await request(client, RemoveComment(pr=REF, comment_id=second.id), timeout=5)
    await request(client, mine(7, "Keep this check", side="LEFT"), timeout=5)

    await request(client, ApproveAction(action_id=pending.id), timeout=5)
    review, earlier = writer.reviews
    assert review["commit_id"] == "abc" and review["comments"] == [
        {"path": "bus.py", "line": 42, "side": "RIGHT", "body": "Race here"},  # the agent's
        {"path": "src/bus.py", "line": 10, "side": "RIGHT", "body": "Why a lock?"},
        {"path": "src/bus.py", "line": 7, "side": "LEFT", "body": "Keep this check"},
    ]
    # Those on an earlier commit go on that commit, in a review of their own.
    assert earlier["commit_id"] == "old" and earlier["event"] == "COMMENT"
    assert [c["body"] for c in earlier["comments"]] == ["Fixed since"]
    assert await store.draft_comments(REF) == []


async def test_rewriting_the_agents_comment_and_replying_in_a_thread(setup):
    store, writer, client, pending = setup
    await request(client, EditProposedComment(pr=REF, index=0, body="A race, I think"), timeout=5)
    assert (await store.get_pending_action(pending.id)).payload["comments"][0]["body"] == (
        "A race, I think"
    )
    with pytest.raises(NotFound):
        await request(client, EditProposedComment(pr=REF, index=5, body="x"), timeout=5)
    await request(client, ApproveAction(action_id=pending.id), timeout=5)
    assert writer.reviews[0]["comments"][0]["body"] == "A race, I think"  # yours, posted

    # A reply in a thread on GitHub goes now.
    with pytest.raises(Invalid):
        await request(client, ReplyToThread(pr=REF, thread_id="T1", body=" "), timeout=5)
    await request(client, ReplyToThread(pr=REF, thread_id="T1", body="Fixed, thanks"), timeout=5)
    assert writer.replies == [("T1", "Fixed, thanks")]
    # A comment of yours there, changed.
    await request(client, EditGitHubComment(pr=REF, comment_id="C1", body="Fixed"), timeout=5)
    assert writer.edits == [("C1", "Fixed")]


async def test_comments_go_to_github_when_you_save_them(setup):
    store, writer, client, pending = setup
    # On lines: now, on their own, at the PR's head.
    on_one = PostLineComment(
        pr=REF, path="bus.py", line=4, side="RIGHT", body="Lock?", head_sha="abc"
    )
    on_several = PostLineComment(
        pr=REF, path="bus.py", line=9, side="RIGHT", body="All of this", head_sha="abc",
        start_line=6, start_side="RIGHT",
    )  # fmt: skip
    await request(client, on_one, timeout=5)
    await request(client, on_several, timeout=5)
    assert writer.line_comments == [
        (REF, {"path": "bus.py", "line": 4, "side": "RIGHT", "body": "Lock?"}, "abc"),
        (REF, {"path": "bus.py", "line": 9, "side": "RIGHT", "body": "All of this",
               "start_line": 6, "start_side": "RIGHT"}, "abc"),
    ]  # fmt: skip
    # On the PR itself: posted, changed; and yours of either kind, deleted.
    with pytest.raises(Invalid):
        await request(client, PostPRComment(pr=REF, body=" "), timeout=5)
    await request(client, PostPRComment(pr=REF, body="Thanks!"), timeout=5)
    await request(client, EditPRComment(pr=REF, comment_id="IC1", body="Thanks a lot!"), timeout=5)
    await request(client, DeleteGitHubComment(pr=REF, comment_id="IC1", on_line=False), timeout=5)
    await request(client, DeleteGitHubComment(pr=REF, comment_id="RC1", on_line=True), timeout=5)
    assert writer.pr_comments == [(REF, "Thanks!")]
    assert writer.pr_edits == [("IC1", "Thanks a lot!")]
    assert writer.deleted == [("IC1", False), ("RC1", True)]
    # GitHub saying no says why.
    writer.fail_with = ["locked"]
    with pytest.raises(Failed, match="locked"):
        await request(client, PostPRComment(pr=REF, body="Hi"), timeout=5)
    # One of the agent's proposed comments, left out of what approving posts.
    with pytest.raises(NotFound):
        await request(client, DropProposedComment(pr=REF, index=3), timeout=5)
    await request(client, DropProposedComment(pr=REF, index=0), timeout=5)
    assert (await store.get_pending_action(pending.id)).payload["comments"] == []


async def test_your_comments_on_their_own(setup):
    store, writer, client, pending = setup
    with pytest.raises(NotFound):
        await request(client, PostComments(pr=REF), timeout=5)
    await request(client, mine(10, "Why a lock here?"), timeout=5)
    writer.fail_with = ["Bad credentials"]
    with pytest.raises(Failed, match="Bad credentials"):
        await request(client, PostComments(pr=REF), timeout=5)
    assert len(await store.draft_comments(REF)) == 1  # kept, to try again

    posted = await request(client, PostComments(pr=REF), timeout=5)
    assert posted.url and writer.reviews[0]["event"] == "COMMENT"
    assert writer.reviews[0]["comments"][0]["body"] == "Why a lock here?"
    assert await store.draft_comments(REF) == []


async def test_a_failed_post_can_be_retried(setup):
    store, writer, client, pending = setup
    writer.fail_with = ["Bad credentials"]
    with pytest.raises(Failed, match="Bad credentials"):
        await request(client, ApproveAction(action_id=pending.id), timeout=5)
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.PENDING
    assert (await store.get_pr(REF)).state == PRState.NEEDS_YOU

    assert (await request(client, ApproveAction(action_id=pending.id), timeout=5)).url


async def test_dismissing(setup):
    store, writer, client, pending = setup
    review = await store.start_review(REF)
    await store.finish_review(review.id, summary="Fine", findings=[])
    assert await request(client, DismissAction(action_id=pending.id), timeout=5) == Done()
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.DISMISSED
    # Nothing's posted, and that's no verdict: it still needs you.
    assert (await store.get_pr(REF)).state == PRState.NEEDS_YOU
    assert writer.reviews == []


async def test_only_a_verdict_makes_the_pr_done(setup):
    store, writer, client, pending = setup
    review = await store.start_review(REF)
    await store.finish_review(review.id, summary="Fine", findings=[])
    await request(client, DismissAction(action_id=pending.id), timeout=5)

    # A comment isn't a verdict: the PR still needs you.
    comment = await store.add_pending_action(
        REF, kind=ActionKind.COMMENT, description="Post 1 comment", payload={"body": "Why?"}
    )
    await request(client, ApproveAction(action_id=comment.id), timeout=5)
    assert writer.reviews[-1]["event"] == "COMMENT"
    assert (await store.get_pr(REF)).state == PRState.NEEDS_YOU

    # Approving it is.
    approval = await store.add_pending_action(REF, kind=ActionKind.APPROVE, description="Approve")
    await request(client, ApproveAction(action_id=approval.id), timeout=5)
    assert (await store.get_pr(REF)).state == PRState.DONE

    # It was of the commit it was at: after new commits, it doesn't count.
    pr = await store.get_pr(REF)
    await store.upsert_pr(replace(pr, head_sha="def"))
    assert await store.resting_state(REF) == PRState.NEEDS_YOU


async def test_approving_with_nothing_proposed(setup):
    store, writer, client, pending = setup
    review = await store.start_review(REF)
    await store.finish_review(review.id, summary="Fine", findings=[])
    with pytest.raises(Busy, match="decide that first"):
        await request(client, ApprovePR(pr=REF), timeout=5)
    await request(client, DismissAction(action_id=pending.id), timeout=5)

    posted = await request(client, ApprovePR(pr=REF), timeout=5)
    assert posted.url.endswith("#review-1")
    assert writer.reviews[-1]["event"] == "APPROVE" and writer.reviews[-1]["body"] == "LGTM"
    assert writer.reviews[-1]["commit_id"] == "abc"
    assert await store.approved_head(REF) == "abc"  # m can merge it
    assert (await store.get_pr(REF)).state == PRState.DONE


async def test_assigning_yourself(setup):
    store, writer, client, pending = setup
    assert await request(client, AssignMe(pr=REF), timeout=5) == Done()
    assert writer.assigned == [REF]
    assert (await store.get_pr(REF)).assigned_to_me is True


def test_the_proposal_shows_what_would_be_posted():
    import io

    from rich.console import Console

    from purr.tui.entries import render_proposal

    def shown(proposal: dict) -> list[str]:
        console = Console(width=100, record=True, file=io.StringIO())
        console.print(render_proposal(proposal))
        return [line.strip() for line in console.export_text().splitlines()]

    lines = shown({
        "kind": "comment", "description": "Comment", "body": "Looks good.",
        "replies": [{"thread_id": "T1", "body": "Done."}],
    })  # fmt: skip
    assert lines[-2:] == ["Looks good.", "↳ reply in thread  Done."]
    assert shown({"kind": "approve", "description": "Approve"})[-1] == "LGTM"


READY = MergeCheck(
    head_sha="abc",
    base="main",
    method="squash",
    mergeable="MERGEABLE",
    merge_state="CLEAN",
    checks=(Check("Unit Tests", CheckState.PASSED),),
    assignees=("me",),
)


@pytest.fixture
async def approved():
    """A PR at abc with an approval waiting to be posted, and GitHub saying it's ready."""
    async with Store.connect(":memory:") as store:
        await store.upsert_pr(
            PR(REF.repo, REF.number, "Add message bus", "someone", "u", "abc", "t")
        )
        await store.set_pr_state(REF, PRState.NEEDS_YOU)
        approval = await store.add_pending_action(REF, kind=ActionKind.APPROVE, description="LGTM")
        github = FakeGitHub()
        writer = FakeWriter(github)  # what it posts is on GitHub
        github.merge_checks[REF] = READY
        worker = GitHubWorker(store=store, github=github, writer=writer, interval=3600)
        client = BaseWorker("client")
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            yield store, github, writer, client, approval
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def test_merging_is_your_final_decision(approved):
    store, github, writer, client, approval = approved
    readiness = await request(client, CheckMerge(pr=REF), timeout=5)
    assert readiness.blockers == ["You haven't approved it"]
    with pytest.raises(Busy, match="haven't approved"):
        await request(client, MergePR(pr=REF, seen=[]), timeout=5)
    await request(client, ApproveAction(action_id=approval.id), timeout=5)
    assert await store.approved_head(REF) == "abc"

    # The docs build failed, and alice (assigned too) hasn't approved: your call.
    github.merge_checks[REF] = replace(
        READY,
        checks=(Check("docs/readthedocs", CheckState.FAILED),),
        assignees=("me", "alice"),
    )
    readiness = await request(client, CheckMerge(pr=REF), timeout=5)
    assert readiness.blockers == []
    assert readiness.concerns == ["docs/readthedocs failed", "alice hasn't approved"]

    # Something new came up after you looked: nothing's merged, you look again.
    github.merge_checks[REF] = replace(
        github.merge_checks[REF], checks=(Check("Unit Tests", CheckState.FAILED),)
    )
    merged = await request(client, MergePR(pr=REF, seen=readiness.concerns), timeout=5)
    assert not merged.merged and merged.readiness.concerns[0] == "Unit Tests failed"
    assert writer.merges == []

    # You decide past what you saw: merged, your way, at the commit you approved.
    seen = merged.readiness.concerns
    merged = await request(client, MergePR(pr=REF, seen=seen), timeout=5)
    assert merged == Merged(merged=True, method="squash")
    assert writer.merges == [(REF, "squash", "abc")]
    assert (await store.get_pr(REF)).gh_state == GHState.MERGED
    with pytest.raises(NotFound, match="isn't open"):
        await request(client, CheckMerge(pr=REF), timeout=5)


async def test_commits_since_you_approved_stop_a_merge(approved):
    store, github, writer, client, approval = approved
    await request(client, ApproveAction(action_id=approval.id), timeout=5)
    github.merge_checks[REF] = replace(READY, head_sha="def")
    with pytest.raises(Busy, match="new commits since you approved"):
        await request(client, MergePR(pr=REF, seen=[]), timeout=5)
    assert writer.merges == []


async def test_a_merge_github_refuses_says_why(approved):
    store, github, writer, client, approval = approved
    await request(client, ApproveAction(action_id=approval.id), timeout=5)
    writer.fail_with = ['Required status check "Build" is failing.']
    with pytest.raises(Failed, match="Required status check"):
        await request(client, MergePR(pr=REF, seen=[]), timeout=5)
    assert (await store.get_pr(REF)).gh_state == GHState.OPEN


async def test_approving_a_push(tmp_path):
    async with Store.connect(":memory:") as store:
        await store.upsert_pr(
            PR(repo=REF.repo, number=REF.number, title="Fix units", author="eric7h", url="u",
               head_sha="abc", updated_at="2026-11-20T10:00:00Z")
        )  # fmt: skip
        review = await store.start_review(REF)
        await store.finish_review(review.id, summary="Fine", findings=[])
        await store.set_pr_state(REF, PRState.NEEDS_YOU)
        payload = {
            "commit": "e0b9581",
            "onto": "abc",
            "repo": "eric7h/pipecat",
            "branch": "fix/units",
            "commits": [{"sha": "e0b9581", "subject": "Fix"}],
        }
        push = await store.add_pending_action(
            REF, kind=ActionKind.PUSH, description="Push e0b9581 to eric7h:fix/units",
            payload=payload,
        )  # fmt: skip
        repos, writer = FakeRepos(tmp_path), FakeWriter()
        worker = GitHubWorker(
            store=store, github=FakeGitHub(), writer=writer, repos=repos, interval=3600
        )
        client = BaseWorker("client")
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            # Refused (someone pushed since): back to pending, to try again or dismiss.
            repos.push_error = GitError("stale info")
            with pytest.raises(Failed, match="Couldn't push: stale info"):
                await request(client, ApproveAction(action_id=push.id), timeout=5)
            assert (await store.get_pending_action(push.id)).status == ActionStatus.PENDING

            repos.push_error = None
            posted = await request(client, ApproveAction(action_id=push.id), timeout=5)
            assert repos.pushed == [(REF, "e0b9581", "eric7h/pipecat", "fix/units", "abc")]
            assert posted.url.endswith("/pull/2841/commits/e0b9581")
            assert writer.reviews == []  # nothing posted
            assert (await store.get_pending_action(push.id)).status == ActionStatus.EXECUTED
            # A push is no verdict: it still needs you.
            pr = await store.get_pr(REF)
            assert pr.state == PRState.NEEDS_YOU
            # What you pushed is its head right away, and its review has seen it: no new
            # commits to re-review when the next poll finds them.
            assert pr.head_sha == "e0b9581"
            assert (await store.latest_review(REF)).head_sha == "e0b9581"
            # So approving next approves what you pushed.
            await request(client, ApprovePR(pr=REF), timeout=5)
            assert writer.reviews[-1]["commit_id"] == "e0b9581"
            assert (await store.get_pr(REF)).state == PRState.DONE
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)
