"""Purr's messages flow over a real Pipecat worker bus the way the app relies on."""

import asyncio
from dataclasses import dataclass
from typing import ClassVar

import pytest
from pipecat.bus import BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.jobs import Busy, PurrError, Request, Unanswered, handles, request
from purr.messages import (
    REVIEW_WORKER,
    PurrEvent,
    ReviewOpened,
    ReviewStarted,
    SessionMessage,
    StartReview,
    describe,
)
from purr.models import PRRef


@dataclass(kw_only=True)
class Crash(Request[ReviewOpened]):
    worker: ClassVar[str] = REVIEW_WORKER


@dataclass(kw_only=True)
class Nobody(Request[ReviewOpened]):
    worker: ClassVar[str] = "nobody"


class FakeReviews(BaseWorker):
    @handles(StartReview)
    async def start_review(self, request: StartReview) -> ReviewOpened:
        if request.pr.number == 2:
            raise Busy(f"{request.pr} is already being reviewed")
        await self.send_bus_message(ReviewStarted(source=self.name, pr=str(request.pr)))
        return ReviewOpened(review_id=request.pr.number * 10)

    @handles(Crash)
    async def crash(self, request: Crash) -> ReviewOpened:
        raise RuntimeError("the disk is full")


class Recorder(BaseWorker):
    def __init__(self, name: str):
        super().__init__(name)
        self.received: asyncio.Queue[BusMessage] = asyncio.Queue()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            await self.received.put(message)


async def run_workers(*workers: BaseWorker):
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(*workers)
    task = asyncio.create_task(runner.run(auto_end=False))
    return runner, task


async def test_a_request_gets_its_reply_and_everyone_hears_the_event():
    reviews = FakeReviews(REVIEW_WORKER)
    tui, voice = Recorder("tui"), Recorder("voice")
    runner, task = await run_workers(reviews, tui, voice)
    try:
        opened = await request(tui, StartReview(pr=PRRef("a/b", 1)), timeout=5)
        assert opened == ReviewOpened(review_id=10)

        # Every participant sees the broadcast, not just the job's requester.
        for listener in (tui, voice):
            event = await asyncio.wait_for(listener.received.get(), 5)
            assert isinstance(event, ReviewStarted)
            assert event.pr == "a/b#1"
            assert event.source == REVIEW_WORKER
    finally:
        await runner.end()
        await asyncio.wait_for(task, 5)


async def test_errors_come_back_as_themselves():
    reviews, tui = FakeReviews(REVIEW_WORKER), Recorder("tui")
    runner, task = await run_workers(reviews, tui)
    try:
        # What the handler raised, class and all.
        with pytest.raises(Busy, match="already being reviewed"):
            await request(tui, StartReview(pr=PRRef("a/b", 2)), timeout=5)
        # Anything else is a PurrError saying what went wrong, not a timeout.
        with pytest.raises(PurrError, match="the disk is full") as raised:
            await request(tui, Crash(), timeout=5)
        assert type(raised.value) is PurrError
        # Not a request the handler can read.
        params = JobParams(name="StartReview", payload={"request": {"pr": "a/b#1"}}, timeout=5)
        async with tui.job(REVIEW_WORKER, params=params) as j:
            pass
        assert j.response["error"]["kind"] == "Invalid"
        # Nobody there.
        with pytest.raises(Unanswered):
            await request(tui, Nobody(), timeout=0.2)
    finally:
        await runner.end()
        await asyncio.wait_for(task, 5)


def test_describe_shows_payload_fields():
    text = describe(ReviewStarted(source="reviews", pr="a/b#1"))
    assert text == "ReviewStarted(pr='a/b#1', plan={}) from reviews"


def test_describe_cuts_long_values():
    result = {"kind": "tool_result", "content": "x" * 5000}
    text = describe(SessionMessage(source="reviews", pr="a/b#1", message=result), width=40)
    assert text == (
        "SessionMessage(pr='a/b#1', message={'kind': 'tool_result', 'content': 'xxx…) from reviews"
    )
