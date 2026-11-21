"""The review worker on a real bus, with a scripted agent."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pipecat.bus import BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.messages import (
    PERMISSION_ANSWER,
    REVIEW_MESSAGE,
    REVIEW_START,
    REVIEW_WORKER,
    ActionPending,
    ActionResolved,
    PermissionRequest,
    PermissionResolved,
    PRChanged,
    PurrEvent,
    ReviewFailed,
    ReviewFinished,
    ReviewStarted,
    SessionMessage,
)
from purr.models import PR, ActionStatus, PRRef, PRState, ReviewStatus
from purr.review.agent import RESUME_PROMPT
from purr.store import Store
from purr.workers import ActivityWorker, ReviewWorker
from tests.fakes import FakeAgent, FakeRepos

REF = PRRef("pipecat-ai/pipecat", 2841)


def make_pr(**kwargs) -> PR:
    kwargs.setdefault("head_sha", "abc123")
    return PR(
        repo=REF.repo,
        number=REF.number,
        title="Add message bus",
        author="someone",
        url="https://github.com/pipecat-ai/pipecat/pull/2841",
        updated_at="2026-11-20T10:00:00Z",
        **kwargs,
    )


class Client(BaseWorker):
    def __init__(self):
        super().__init__("client")
        self.events: list[PurrEvent] = []

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            self.events.append(message)

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    async def request(self, name: str, payload: dict) -> dict:
        params = JobParams(name=name, payload=payload, timeout=5)
        async with self.job(REVIEW_WORKER, params=params) as j:
            pass
        return j.response


async def eventually(check: Callable[[], Awaitable[bool] | bool], timeout: float = 5) -> None:
    async def wait():
        while True:
            result = check()
            if await result if asyncio.iscoroutine(result) else result:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


class Setup:
    def __init__(self, store, agent, repos, client, runner):
        self.store, self.agent, self.repos, self.client, self.runner = (
            store,
            agent,
            repos,
            client,
            runner,
        )

    async def state(self) -> PRState:
        return (await self.store.get_pr(REF)).state


@pytest.fixture
async def make(tmp_path):
    """Builds a running setup; ``before_start`` can seed the store first."""
    cleanups = []

    async def build(before_start=None, auto_review=True, permission_wait=300) -> Setup:
        store = await Store.open(":memory:")
        agent, repos, client = FakeAgent(), FakeRepos(tmp_path), Client()
        if before_start:
            await before_start(store, agent)
        worker = ReviewWorker(
            store=store,
            repos=repos,
            agent=agent,
            auto_review=auto_review,
            permission_wait=permission_wait,
        )
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(ActivityWorker(store=store), worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))

        async def cleanup():
            await runner.end()
            await asyncio.wait_for(task, 5)
            await store.close()

        cleanups.append(cleanup)
        await asyncio.sleep(0.05)
        return Setup(store, agent, repos, client, runner)

    yield build
    for cleanup in cleanups:
        await cleanup()


async def test_a_review_produces_findings_and_a_pending_action(make):
    s = await make()
    await s.store.upsert_pr(make_pr())

    response = await s.client.request(REVIEW_START, {"pr": str(REF)})
    assert "review_id" in response
    await eventually(lambda: s.client.of(ReviewFinished))

    review = await s.store.latest_review(REF)
    assert review.status == ReviewStatus.FINISHED
    assert review.summary == "Clean refactor, one race."
    assert review.worktree_path == str(s.repos.worktree_path(REF))
    assert [(f.file, f.line) for f in await s.store.findings(review.id)] == [("bus.py", 42)]
    [action] = await s.store.pending_actions(REF)
    assert action.payload["comments"][0]["body"] == "Race here"
    await eventually(lambda: s.state())
    assert await s.state() == PRState.NEEDS_YOU

    assert s.client.of(ReviewStarted) and s.client.of(ActionPending) and s.client.of(SessionMessage)
    assert s.repos.checkouts == [(REF, "abc123")]
    assert s.agent.runs[0]["resume"] is False
    assert s.agent.runs[0]["session_id"] == review.session_id

    # Starting again supersedes the pending action, and re-reviews.
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    assert (await s.store.get_pending_action(action.id)).status == ActionStatus.DISMISSED
    assert s.client.of(ActionResolved)


async def test_prs_assigned_to_you_are_reviewed_automatically(make):
    s = await make()
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.send_bus_message(
        PRChanged(source="github", pr=str(REF), change="assigned", title="Add message bus")
    )
    await eventually(lambda: s.client.of(ReviewFinished))


async def test_already_assigned_prs_are_reviewed_on_startup(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr(assigned_to_me=True))

    s = await make(before_start=seed)
    await eventually(lambda: s.client.of(ReviewFinished))


async def test_no_auto_review_when_turned_off(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr(assigned_to_me=True))

    s = await make(before_start=seed, auto_review=False)
    await asyncio.sleep(0.1)
    assert s.agent.runs == []


async def test_a_failed_review_needs_you(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.fail_with = "out of credits"

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFailed))
    review = await s.store.latest_review(REF)
    assert review.status == ReviewStatus.FAILED and review.error == "out of credits"
    await eventually(lambda: s.state())
    assert await s.state() == PRState.NEEDS_YOU


async def test_one_review_at_a_time_per_pr(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.gate = asyncio.Event()

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    response = await s.client.request(REVIEW_START, {"pr": str(REF)})
    assert "already being reviewed" in response["error"]
    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "why?"})
    assert "still working" in response["error"]
    s.agent.gate.set()
    await eventually(lambda: s.client.of(ReviewFinished))


async def test_follow_up_continues_the_session(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))

    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "Is the race real?"})
    assert response == {"ok": True}
    await eventually(lambda: len(s.agent.runs) == 2)
    followup = s.agent.runs[1]
    assert followup["resume"] is True and followup["prompt"] == "Is the race real?"
    assert followup["session_id"] == s.agent.runs[0]["session_id"]


async def test_interrupted_reviews_resume_on_startup(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr())
        review = await store.start_review(REF)
        await store.set_review_session(review.id, "11111111-1111-1111-1111-111111111111")
        await store.set_pr_state(REF, PRState.REVIEWING)
        agent.sessions.add("11111111-1111-1111-1111-111111111111")

    s = await make(before_start=seed)
    await eventually(lambda: s.client.of(ReviewFinished))
    run = s.agent.runs[0]
    assert run["resume"] is True and run["prompt"] == RESUME_PROMPT


async def test_stopping_leaves_the_review_to_resume(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.gate = asyncio.Event()
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.agent.runs)

    await s.runner.end()
    await asyncio.sleep(0.1)
    assert (await s.store.latest_review(REF)).status == ReviewStatus.RUNNING


async def test_the_agent_waits_for_permission(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.ask = ("Bash", {"command": "uv run pytest"}, "only read-only commands are pre-approved")

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(PermissionRequest))
    [request] = s.client.of(PermissionRequest)
    assert request.description == "$ uv run pytest"
    assert await s.state() == PRState.NEEDS_YOU
    assert not s.client.of(ReviewFinished)

    answer = {"request_id": request.request_id, "allow": True}
    assert await s.client.request(PERMISSION_ANSWER, answer) == {"ok": True}
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.answers == [True]
    assert "error" in await s.client.request(PERMISSION_ANSWER, answer)  # already answered


async def test_new_commits_get_a_rereview_in_the_same_session(make):
    s = await make()
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    first = s.agent.runs[0]
    assert (await s.store.latest_review(REF)).head_sha == "abc123"

    # The author pushes; the poller notices.
    await s.store.upsert_pr(make_pr(assigned_to_me=True, head_sha="def456"))
    await s.client.send_bus_message(
        PRChanged(source="github", pr=str(REF), change="new_commits", title="Add message bus")
    )
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    rereview = s.agent.runs[1]
    assert rereview["resume"] is True and rereview["session_id"] == first["session_id"]
    assert "git diff abc123 def456" in rereview["prompt"]
    assert (await s.store.latest_review(REF)).head_sha == "def456"
    assert s.repos.checkouts[-1] == (REF, "def456")

    # Same head again: nothing to re-review.
    worker = s.runner.get_worker(REVIEW_WORKER)
    assert await worker.rereview(REF) is False


async def test_an_unanswered_permission_request_lets_the_agent_carry_on(make):
    s = await make(permission_wait=0.1)
    await s.store.upsert_pr(make_pr())
    s.agent.ask = ("Bash", {"command": "uv --version"}, "not pre-approved")

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.answers == [None]  # "nobody answered", not "no"
    [resolved] = s.client.of(PermissionResolved)
    assert resolved.answered is False and resolved.description == "$ uv --version"


async def test_a_pr_left_reviewing_by_a_cut_short_follow_up_is_settled(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr())
        review = await store.start_review(REF)
        await store.finish_review(review.id, summary="ok", findings=[])
        await store.set_pr_state(REF, PRState.REVIEWING)

    s = await make(before_start=seed)
    await eventually(lambda: s.state())
    for _ in range(100):
        if await s.state() == PRState.DONE:
            break
        await asyncio.sleep(0.01)
    assert await s.state() == PRState.DONE


async def test_reviews_get_the_repo_guidelines(make):
    s = await make()
    s.repos.repo_guidelines = "### AGENTS.md\n\nUse Google-style docstrings."
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert "Google-style" in s.agent.runs[0]["guidelines"]
