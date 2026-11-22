"""The recap worker: writes recaps and remembers when the user was last around."""

from datetime import UTC, datetime

from loguru import logger
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker

from purr.jobs import handles
from purr.messages import RECAP_WORKER, MakeRecap, RecapMade, RecapReady, UserActive
from purr.news import is_notable
from purr.recap import RecapAgent, fallback_recap, recap_notes
from purr.store import Store

LAST_ACTIVE = "last_active_at"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RecapWorker(BaseWorker):
    """Handles ``recap.create {since}`` and records ``user.active`` as the last-active time.

    The TUI (and later the voice agent) decide when the user is back; this
    worker turns the notable activity since then into a recap.
    """

    def __init__(self, name: str = RECAP_WORKER, *, store: Store, agent: RecapAgent):
        super().__init__(name)
        self._store = store
        self._agent = agent

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, UserActive):
            await self._store.set_meta(LAST_ACTIVE, now())

    @handles(MakeRecap)
    async def make_recap(self, request: MakeRecap) -> RecapMade:
        since = request.since
        activities = [a for a in await self._store.activity(since=since) if is_notable(a)]
        if not activities:
            return RecapMade(recap_id=None)
        refs = list(dict.fromkeys(a.pr for a in activities if a.pr))
        prs = {ref: pr for ref in refs if (pr := await self._store.get_pr(ref))}
        pending = {ref: await self._store.pending_actions(ref) for ref in prs}
        try:
            headline, items = await self._agent.summarize(recap_notes(activities, prs, pending))
        except Exception as e:
            logger.warning(f"Couldn't write the recap with the agent ({e}); listing events instead")
            headline, items = fallback_recap(activities)
        recap = await self._store.add_recap(
            since=since, until=now(), headline=headline, items=items
        )
        await self.send_bus_message(
            RecapReady(source=self.name, recap_id=recap.id, headline=recap.headline)
        )
        return RecapMade(recap_id=recap.id)
