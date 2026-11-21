"""The TUI's seat on the bus: forwards events into Textual and runs jobs for it."""

from pipecat.bus import BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from textual.app import App
from textual.message import Message

from purr.messages import (
    ACTION_APPROVE,
    ACTION_DISMISS,
    GITHUB_WORKER,
    PERMISSION_ANSWER,
    PR_ASSIGN,
    RECAP_CREATE,
    RECAP_WORKER,
    REPO_ADD,
    REPO_REMOVE,
    REVIEW_MESSAGE,
    REVIEW_START,
    REVIEW_WORKER,
    PurrEvent,
    UserActive,
)
from purr.models import PRRef


class BusEvent(Message):
    """A Purr event from the bus, delivered to the Textual app."""

    def __init__(self, event: PurrEvent) -> None:
        super().__init__()
        self.event = event


class RequestError(Exception):
    """A job came back with an error the user should see."""


class UIWorker(BaseWorker):
    """Bus participant for the TUI.

    Every :class:`PurrEvent` is posted to the app as a :class:`BusEvent`, and
    screens call the methods here to dispatch jobs, the same way the voice
    agent will.
    """

    def __init__(self, app: App, name: str = "tui"):
        super().__init__(name)
        self._app = app

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            self._app.post_message(BusEvent(message))

    async def add_repo(self, repo: str) -> str:
        """Watch a repo (``owner/name`` or URL). Returns GitHub's spelling of its name."""
        response = await self._job(GITHUB_WORKER, REPO_ADD, {"repo": repo})
        return response["repo"]

    async def remove_repo(self, repo: str) -> None:
        await self._job(GITHUB_WORKER, REPO_REMOVE, {"repo": repo})

    async def start_review(self, pr: PRRef) -> None:
        await self._job(REVIEW_WORKER, REVIEW_START, {"pr": str(pr)})

    async def assign(self, pr: PRRef) -> None:
        await self._job(GITHUB_WORKER, PR_ASSIGN, {"pr": str(pr)})

    async def send_message(self, pr: PRRef, text: str) -> None:
        await self._job(REVIEW_WORKER, REVIEW_MESSAGE, {"pr": str(pr), "text": text})

    async def approve(self, action_id: str) -> str:
        return (await self._job(GITHUB_WORKER, ACTION_APPROVE, {"action_id": action_id}))["url"]

    async def dismiss(self, action_id: str) -> None:
        await self._job(GITHUB_WORKER, ACTION_DISMISS, {"action_id": action_id})

    async def create_recap(self, since: str) -> int | None:
        """Recap what happened since ``since``; None if nothing notable did."""
        response = await self._job(RECAP_WORKER, RECAP_CREATE, {"since": since}, timeout=180)
        return response.get("recap_id")

    async def active(self) -> None:
        await self.send_bus_message(UserActive(source=self.name))

    async def answer(self, request_id: str, allow: bool) -> None:
        payload = {"request_id": request_id, "allow": allow}
        await self._job(REVIEW_WORKER, PERMISSION_ANSWER, payload)

    async def _job(self, worker: str, name: str, payload: dict, timeout: float = 60) -> dict:
        params = JobParams(name=name, payload=payload, timeout=timeout)
        async with self.job(worker, params=params) as job:
            pass
        response = job.response or {}
        if "error" in response:
            raise RequestError(response["error"])
        return response
