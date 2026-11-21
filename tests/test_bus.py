"""Purr's messages flow over a real Pipecat worker bus the way the app relies on."""

import asyncio
from dataclasses import dataclass

from pipecat.bus import BusJobRequestMessage, BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.messages import (
    REVIEW_START,
    REVIEW_WORKER,
    PurrCommand,
    PurrEvent,
    ReviewStarted,
    describe,
)


@dataclass(kw_only=True)
class ApproveAction(PurrCommand):
    action_id: str


class FakeReviews(BaseWorker):
    @job(name=REVIEW_START)
    async def on_review_start(self, message: BusJobRequestMessage):
        pr = message.payload["pr"]
        await self.send_bus_message(ReviewStarted(source=self.name, pr=pr))
        await self.send_job_response(message.job_id, {"pr": pr})


class Recorder(BaseWorker):
    def __init__(self, name: str):
        super().__init__(name)
        self.received: asyncio.Queue[BusMessage] = asyncio.Queue()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, (PurrEvent, PurrCommand)) and message.target in (None, self.name):
            await self.received.put(message)


async def run_workers(*workers: BaseWorker):
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(*workers)
    task = asyncio.create_task(runner.run(auto_end=False))
    return runner, task


async def test_review_job_and_broadcast_event():
    reviews = FakeReviews(REVIEW_WORKER)
    tui, voice = Recorder("tui"), Recorder("voice")
    runner, task = await run_workers(reviews, tui, voice)
    try:
        async with tui.job(
            REVIEW_WORKER,
            params=JobParams(name=REVIEW_START, payload={"pr": "a/b#1"}, timeout=5),
        ) as j:
            pass
        assert j.response == {"pr": "a/b#1"}

        # Every participant sees the broadcast, not just the job's requester.
        for listener in (tui, voice):
            event = await asyncio.wait_for(listener.received.get(), 5)
            assert isinstance(event, ReviewStarted)
            assert event.pr == "a/b#1"
            assert event.source == REVIEW_WORKER
    finally:
        await runner.end()
        await asyncio.wait_for(task, 5)


async def test_targeted_command_reaches_its_target():
    github, other = Recorder("github"), Recorder("other")
    runner, task = await run_workers(github, other)
    try:
        await other.send_bus_message(ApproveAction(source="other", target="github", action_id="x"))
        command = await asyncio.wait_for(github.received.get(), 5)
        assert isinstance(command, ApproveAction)
        assert command.action_id == "x"
        assert other.received.empty()
    finally:
        await runner.end()
        await asyncio.wait_for(task, 5)


def test_describe_shows_payload_fields():
    text = describe(ApproveAction(source="tui", target="github", action_id="x"))
    assert text == "ApproveAction(action_id='x') from tui → github"
