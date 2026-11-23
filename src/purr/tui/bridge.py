"""The TUI's seat on the bus: forwards events into Textual and sends requests for it."""

from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker
from textual.app import App
from textual.message import Message

from purr.jobs import R, Request, request
from purr.messages import PurrEvent, UserActive


class BusEvent(Message):
    """A Purr event from the bus, delivered to the Textual app."""

    def __init__(self, event: PurrEvent) -> None:
        super().__init__()
        self.event = event


class UIWorker(BaseWorker):
    """Bus participant for the TUI.

    Every :class:`PurrEvent` is posted to the app as a :class:`BusEvent`, and
    screens send requests through :meth:`request` (see :mod:`purr.jobs`), the same
    way the voice agent will.
    """

    def __init__(self, app: App, name: str = "tui"):
        super().__init__(name)
        self._app = app

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            self._app.post_message(BusEvent(message))

    async def request(self, req: Request[R]) -> R:
        """Send ``req`` and wait for its reply; raises the PurrError it came back with."""
        return await request(self, req)

    async def active(self) -> None:
        await self.send_bus_message(UserActive(source=self.name))
