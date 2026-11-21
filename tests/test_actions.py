"""Approving and dismissing pending actions."""

import asyncio

import pytest
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.actions import ActionError, execute
from purr.messages import ACTION_APPROVE, ACTION_DISMISS, GITHUB_WORKER, PR_ASSIGN
from purr.models import PR, ActionKind, ActionStatus, PendingAction, PRRef, PRState
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
    assert writer.reviews[0]["body"] == ""  # approving needs no text
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


async def decide(client, name, action_id) -> dict:
    params = JobParams(name=name, payload={"action_id": action_id}, timeout=5)
    async with client.job(GITHUB_WORKER, params=params) as j:
        pass
    return j.response


async def test_approving_posts_once_and_the_pr_is_done(setup):
    store, writer, client, pending = setup
    response = await decide(client, ACTION_APPROVE, pending.id)
    assert response["url"].endswith("#review-1")
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.EXECUTED
    assert (await store.get_pr(REF)).state == PRState.DONE

    response = await decide(client, ACTION_APPROVE, pending.id)
    assert "already decided" in response["error"]
    assert len(writer.reviews) == 1


async def test_a_failed_post_can_be_retried(setup):
    store, writer, client, pending = setup
    writer.fail_with = ["Bad credentials"]
    response = await decide(client, ACTION_APPROVE, pending.id)
    assert "Bad credentials" in response["error"]
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.PENDING
    assert (await store.get_pr(REF)).state == PRState.NEEDS_YOU

    assert "url" in await decide(client, ACTION_APPROVE, pending.id)


async def test_dismissing(setup):
    store, writer, client, pending = setup
    assert await decide(client, ACTION_DISMISS, pending.id) == {"ok": True}
    assert (await store.get_pending_action(pending.id)).status == ActionStatus.DISMISSED
    assert (await store.get_pr(REF)).state == PRState.DONE
    assert writer.reviews == []


async def test_assigning_yourself(setup):
    store, writer, client, pending = setup
    params = JobParams(name=PR_ASSIGN, payload={"pr": str(REF)}, timeout=5)
    async with client.job(GITHUB_WORKER, params=params) as j:
        pass
    assert j.response == {"ok": True}
    assert writer.assigned == [REF]
    assert (await store.get_pr(REF)).assigned_to_me is True
