"""A bus participant that logs every Purr event and command."""

from loguru import logger
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker

from purr.messages import PurrCommand, PurrEvent, describe


class EventLogWorker(BaseWorker):
    """Logs Purr's events and commands, so headless runs show what's happening."""

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, (PurrEvent, PurrCommand)):
            logger.info(describe(message))
