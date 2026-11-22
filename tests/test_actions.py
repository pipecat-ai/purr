"""Approving and dismissing pending actions."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.actions import ActionError, execute
from purr.jobs import Busy, Done, Failed, Invalid, NotFound, request
from purr.messages import (
    ApproveAction,
    AssignMe,
    CheckMerge,
    DismissAction,
    Merged,
    MergePR,
    PostComments,
    RemoveComment,
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
from tests.fakes import FakeGitHub, FakeWriter

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
    assert await request(client, DismissAction(action_id=pending.id), timeout=5) == Done()
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.DISMISSED
    assert (await store.get_pr(REF)).state == PRState.DONE
    assert writer.reviews == []


async def test_assigning_yourself(setup):
    store, writer, client, pending = setup
    assert await request(client, AssignMe(pr=REF), timeout=5) == Done()
    assert writer.assigned == [REF]
    assert (await store.get_pr(REF)).assigned_to_me is True


async def test_what_purr_posts_says_so():
    sig = "Sent from Purr."
    writer = FakeWriter()
    replies = [{"thread_id": "T1", "body": "Fixed now, thanks."}]
    await execute(
        writer, action(body="Two things.", comments=COMMENTS, replies=replies),
        head_sha="abc", signature=sig,
    )  # fmt: skip
    [review] = writer.reviews
    assert review["body"] == "Two things.\n\n<sub>Sent from Purr.</sub>"
    assert review["comments"][0]["body"] == "Race here"  # the review's text already says it
    assert writer.replies == [("T1", "Fixed now, thanks.\n\n<sub>Sent from Purr.</sub>")]

    # An approval with nothing else to say: LGTM, and where it came from.
    writer = FakeWriter()
    await execute(writer, action(ActionKind.APPROVE), head_sha="abc", signature=sig)
    assert writer.reviews[0]["body"] == "LGTM\n\n<sub>Sent from Purr.</sub>"
    writer = FakeWriter()
    await execute(writer, action(ActionKind.APPROVE), head_sha="abc")
    assert writer.reviews[0]["body"] == "LGTM"

    # Comments folded into the text: the signature stays last.
    writer = FakeWriter()
    writer.fail_with = ["Unprocessable Entity: Line could not be resolved"]
    await execute(
        writer, action(body="Two things.", comments=COMMENTS), head_sha="abc", signature=sig
    )
    assert writer.reviews[0]["body"] == (
        "Two things.\n\n- `bus.py:42`: Race here\n\n<sub>Sent from Purr.</sub>"
    )

    # No signature configured: posted as written.
    writer = FakeWriter()
    await execute(writer, action(body="Two things."), head_sha="abc", signature="")
    assert writer.reviews[0]["body"] == "Two things."


def test_the_proposal_shows_the_signature():
    import io

    from rich.console import Console

    from purr.tui.session import render_proposal

    proposal = {
        "kind": "comment",
        "description": "Comment",
        "body": "Looks good.",
        "replies": [{"thread_id": "T1", "body": "Done."}],
    }
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(render_proposal(proposal, "Sent from Purr."))
    lines = [line.strip() for line in console.export_text().splitlines()]
    assert lines[lines.index("Looks good.") + 1] == "Sent from Purr."
    assert "↳ reply in thread  Done.  Sent from Purr." in lines


def test_the_default_signature_links_to_purr_and_reads_plainly_in_purr():
    import io

    from rich.console import Console

    from purr.config import Config
    from purr.tui.session import render_proposal

    signature = Config(data_dir=Path("/tmp")).signature
    assert signature == "Sent from [Purr](https://github.com/pipecat-ai/purr)."
    console = Console(width=100, record=True, file=io.StringIO())
    console.print(render_proposal({"kind": "approve", "description": "Approve"}, signature))
    lines = [line.strip() for line in console.export_text().splitlines()]
    assert lines[-2:] == ["LGTM", "Sent from Purr."]


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
        github, writer = FakeGitHub(), FakeWriter()
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
    assert readiness.blockers == ["You haven't approved it here"]
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
