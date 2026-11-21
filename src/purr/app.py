"""The Purr process: one store and one worker runner on a local bus."""

import asyncio

from loguru import logger
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.actions import GitHubWriter
from purr.config import Config
from purr.git import GitRepos
from purr.github import GitHub
from purr.recap import ClaudeRecapAgent, RecapAgent
from purr.review.agent import ClaudeReviewAgent, ReviewAgent
from purr.store import Store
from purr.workers import ActivityWorker, GitHubWorker, RecapWorker, ReviewWorker


class Purr:
    """Owns the store and the worker runner, and wires up Purr's workers.

    Every component (poller, review workers, TUI, voice agent) is a worker on
    the runner's in-process bus. Start it from the event loop that hosts
    everything: ``run()`` for a headless process, or ``start()`` / ``stop()``
    when a UI (e.g. Textual) owns the loop and adds its own workers.

    Args:
        config: Where data lives and how often to poll.
        github: GitHub client (a fake in tests).
        agent: Runs review sessions (a fake in tests).
        repos: Clones and worktrees (a fake in tests).
        writer: Posts approved actions to GitHub (a fake in tests).
        recap_agent: Writes "while you were away" recaps (a fake in tests).
        handle_sigint: Let the runner stop on Ctrl+C. Turn off when a UI
            owns the terminal and handles keys itself.
    """

    def __init__(
        self,
        config: Config,
        *,
        github: GitHub | None = None,
        agent: ReviewAgent | None = None,
        repos: GitRepos | None = None,
        writer: GitHubWriter | None = None,
        recap_agent: RecapAgent | None = None,
        handle_sigint: bool = True,
    ):
        self._config = config
        self._github = github or GitHub()
        self._agent = agent or ClaudeReviewAgent(
            model=config.review_model, max_budget_usd=config.review_budget_usd
        )
        self._repos = repos or GitRepos(config.data_dir)
        self._writer = writer
        self._recap_agent = recap_agent or ClaudeRecapAgent(model=config.review_model)
        self._handle_sigint = handle_sigint
        self._store: Store | None = None
        self._runner: WorkerRunner | None = None
        self._task: asyncio.Task | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            raise RuntimeError("Purr is not running")
        return self._store

    @property
    def config(self) -> Config:
        return self._config

    @property
    def agent(self) -> ReviewAgent:
        return self._agent

    @property
    def runner(self) -> WorkerRunner:
        if self._runner is None:
            raise RuntimeError("Purr is not running")
        return self._runner

    async def start(self, *workers: BaseWorker) -> None:
        """Open the store and start the runner with Purr's workers plus ``workers``."""
        self._config.data_dir.mkdir(parents=True, exist_ok=True)
        self._store = await Store.open(self._config.db_path)
        logger.info(f"Purr data in {self._config.data_dir}")
        self._runner = WorkerRunner(name="purr", handle_sigint=self._handle_sigint)
        await self._runner.add_workers(*self._create_workers(self._store), *workers)
        self._task = asyncio.create_task(self._runner.run(auto_end=False))

    async def wait(self) -> None:
        """Wait until the runner ends (``stop()``, SIGINT, or a bus end/cancel), then clean up."""
        try:
            if self._task:
                await self._task
        finally:
            self._task = None
            self._runner = None
            if self._store:
                await self._store.close()
                self._store = None

    async def stop(self) -> None:
        if self._runner:
            await self._runner.end(reason="purr stopping")
        await self.wait()

    async def run(self, *workers: BaseWorker) -> None:
        """Run until stopped (``stop()``, SIGINT, or a bus end/cancel message)."""
        await self.start(*workers)
        await self.wait()

    def _create_workers(self, store: Store) -> list[BaseWorker]:
        return [
            ActivityWorker(store=store),
            GitHubWorker(
                store=store,
                github=self._github,
                writer=self._writer,
                interval=self._config.poll_interval,
            ),
            ReviewWorker(
                store=store,
                repos=self._repos,
                agent=self._agent,
                auto_review=self._config.auto_review,
            ),
            RecapWorker(store=store, agent=self._recap_agent),
        ]
