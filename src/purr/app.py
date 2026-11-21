"""The Purr process: one store and one worker runner on a local bus."""

from loguru import logger
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.config import Config
from purr.store import Store
from purr.workers import EventLogWorker


class Purr:
    """Owns the store and the worker runner, and wires up Purr's workers.

    Every component (poller, review workers, TUI, voice agent) is a worker on
    the runner's in-process bus. ``run()`` must be awaited from the event loop
    that will host everything, so an embedding UI (e.g. Textual) runs it as a
    background task on its own loop.

    Args:
        config: Where data lives.
        handle_sigint: Let the runner stop on Ctrl+C. Turn off when a UI
            owns the terminal and handles keys itself.
    """

    def __init__(self, config: Config, *, handle_sigint: bool = True):
        self._config = config
        self._handle_sigint = handle_sigint
        self._runner: WorkerRunner | None = None

    @property
    def runner(self) -> WorkerRunner:
        if self._runner is None:
            raise RuntimeError("Purr is not running")
        return self._runner

    async def run(self) -> None:
        """Run until stopped (``stop()``, SIGINT, or a bus end/cancel message)."""
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        async with Store.connect(self._config.db_path) as store:
            logger.info(f"Purr data in {self._config.data_dir}")
            self._runner = WorkerRunner(name="purr", handle_sigint=self._handle_sigint)
            await self._runner.add_workers(*self._create_workers(store))
            try:
                await self._runner.run(auto_end=False)
            finally:
                self._runner = None

    async def stop(self) -> None:
        if self._runner:
            await self._runner.end(reason="purr stopping")

    def _create_workers(self, store: Store) -> list[BaseWorker]:
        return [EventLogWorker("event-log")]
