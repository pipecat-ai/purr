"""Records Purr's events in the activity log, and logs them."""

from loguru import logger
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker

from purr.messages import PurrEvent, describe, payload
from purr.models import PRRef
from purr.store import Store


class ActivityWorker(BaseWorker):
    """Appends every recorded :class:`PurrEvent` to the store's activity log.

    The activity log is what news that survives restarts, and each PR's timeline,
    are built from.
    Events are also logged, briefly, so headless runs show what's happening.
    """

    def __init__(self, name: str = "activity", *, store: Store):
        super().__init__(name)
        self._store = store

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if not isinstance(message, PurrEvent):
            return
        logger.info(describe(message))
        if message.recorded:
            data = payload(message)
            pr = data.pop("pr", None)
            await self._store.record_activity(
                message.kind, pr=PRRef.parse(pr) if pr else None, data=data
            )
