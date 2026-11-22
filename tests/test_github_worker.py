"""The GitHub worker on a real bus, with GitHub faked out."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.jobs import Invalid, NotFound, request
from purr.messages import (
    PRChanged,
    PRListUpdated,
    PurrEvent,
    Synced,
    UnwatchRepo,
    Watching,
    WatchRepo,
)
from purr.models import PRChange, PRRef
from purr.store import Store
from purr.workers import ActivityWorker, GitHubWorker
from tests.fakes import FakeGitHub, remote


class Client(BaseWorker):
    def __init__(self):
        super().__init__("client")
        self.events: asyncio.Queue[PurrEvent] = asyncio.Queue()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            await self.events.put(message)

    async def next_event(self, kind: type[PurrEvent]) -> PurrEvent:
        while True:
            event = await asyncio.wait_for(self.events.get(), 5)
            if isinstance(event, kind):
                return event


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 5) -> None:
    async def wait():
        while not await check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
async def setup():
    async with Store.connect(":memory:") as store:
        github = FakeGitHub()
        worker = GitHubWorker(store=store, github=github, interval=3600)
        client = Client()
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(ActivityWorker(store=store), worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            yield store, github, worker, client
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def test_add_poll_and_remove_a_repo(setup):
    store, github, worker, client = setup
    github.open["pipecat-ai/pipecat"] = [remote(1)]

    # Added by URL, stored under GitHub's spelling.
    watching = await request(client, WatchRepo(repo="https://github.com/Pipecat-AI/pipecat"))
    assert watching == Watching(repo="pipecat-ai/pipecat")
    await client.next_event(PRListUpdated)

    # The first sync fills the pool quietly.
    async def synced():
        repo = await store.get_repo("pipecat-ai/pipecat")
        return repo is not None and repo.synced_at is not None

    await eventually(synced)
    assert [pr.number for pr in await store.list_prs()] == [1]

    # Later polls are incremental, report what's new, and it lands in the activity log.
    github.updates["pipecat-ai/pipecat"] = [remote(2)]
    await worker.poll()
    full, incremental = github.calls[-1]
    assert full == [] and list(incremental) == ["pipecat-ai/pipecat"]
    event = await client.next_event(PRChanged)
    assert (event.pr, event.change) == ("pipecat-ai/pipecat#2", PRChange.POOLED)
    await client.next_event(PRListUpdated)
    # Every poll ends saying so (reviews of PRs assigned to you wait for it).
    await client.next_event(Synced)

    async def recorded():
        return len(await store.activity(pr=PRRef("pipecat-ai/pipecat", 2))) == 1

    await eventually(recorded)

    await request(client, UnwatchRepo(repo="pipecat-ai/pipecat"))
    assert await store.list_repos() == []
    assert await store.list_prs() == []


async def test_adding_an_unknown_repo_is_rejected(setup):
    store, _, _, client = setup
    with pytest.raises(NotFound, match="Can't find nobody/nothing"):
        await request(client, WatchRepo(repo="nobody/nothing"))
    with pytest.raises(Invalid, match="not a GitHub repo"):
        await request(client, WatchRepo(repo="not a repo"))
    with pytest.raises(NotFound, match="isn't being watched"):
        await request(client, UnwatchRepo(repo="nobody/nothing"))
    assert await store.list_repos() == []
