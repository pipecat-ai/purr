"""Recaps: what's notable, the notes the agent gets, and the worker."""

import asyncio

import pytest
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.jobs import request
from purr.messages import MakeRecap, UserActive
from purr.models import PR, PRRef
from purr.news import is_notable
from purr.recap import fallback_recap, recap_notes
from purr.store import Store
from purr.workers import RecapWorker
from purr.workers.recap import LAST_ACTIVE
from tests.fakes import FakeRecapAgent

REF = PRRef("pipecat-ai/pipecat", 2841)


def make_pr() -> PR:
    return PR(
        repo=REF.repo,
        number=REF.number,
        title="Add message bus",
        author="someone",
        url="u",
        head_sha="abc",
        updated_at="2026-11-20T10:00:00Z",
    )


async def seed(store: Store) -> None:
    await store.upsert_pr(make_pr())
    await store.record_activity("pr.changed", pr=REF, data={"change": "unpooled", "title": "x"})
    await store.record_activity(
        "review.finished", pr=REF, data={"summary": "One race.", "findings": []}
    )
    await store.record_activity("action.pending", pr=REF, data={"description": "Request changes"})


async def test_notes_and_fallback():
    async with Store.connect(":memory:") as store:
        await seed(store)
        activities = [a for a in await store.activity() if is_notable(a)]
        assert [a.kind for a in activities] == ["review.finished", "action.pending"]

        notes = recap_notes(activities, {REF: make_pr()}, {REF: []})
        assert 'pipecat-ai/pipecat#2841 ("Add message bus"): review finished: One race.' in notes
        assert "agent proposes: Request changes" in notes

        headline, items = fallback_recap(activities)
        assert headline == "2 things happened while you were away."
        assert items == [
            {"pr": str(REF), "text": "Agent proposes: request changes", "needs_you": True}
        ]


@pytest.fixture
async def setup():
    async with Store.connect(":memory:") as store:
        agent = FakeRecapAgent()
        client = BaseWorker("client")
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(RecapWorker(store=store, agent=agent), client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            yield store, agent, client
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def create(client, since="") -> int | None:
    return (await request(client, MakeRecap(since=since), timeout=5)).recap_id


async def test_recap_worker(setup):
    store, agent, client = setup
    assert await create(client) is None  # nothing happened

    await seed(store)
    recap = await store.get_recap(await create(client))
    assert recap.headline == "1 PRs need you."
    assert recap.items[0]["pr"] == str(REF)
    assert (await store.latest_recap()).id == recap.id

    agent.fail = True
    recap = await store.get_recap(await create(client))
    assert recap.headline == "2 things happened while you were away."


async def test_user_active_is_remembered(setup):
    store, _, client = setup
    await client.send_bus_message(UserActive(source="client"))
    for _ in range(100):
        if await store.get_meta(LAST_ACTIVE):
            break
        await asyncio.sleep(0.01)
    assert await store.get_meta(LAST_ACTIVE)
