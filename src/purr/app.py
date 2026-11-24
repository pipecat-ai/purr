"""The Purr process: one store and one worker runner on a local bus."""

import asyncio

from loguru import logger
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.actions import GitHubWriter
from purr.config import Config
from purr.git import GitRepos
from purr.github import GitHub
from purr.lead import ClaudeLead, LeadAgent
from purr.recap import ClaudeRecapAgent, RecapAgent
from purr.review.agent import ClaudeReviewAgent, ReviewAgent
from purr.review.instructions import ReviewInstructions
from purr.review.rules import RepoRules
from purr.store import Store
from purr.suggest import ClaudeSuggester, Suggester
from purr.upnext import ClaudePicker, Picker
from purr.workers import (
    ActivityWorker,
    GitHubWorker,
    GitWorker,
    LeadWorker,
    RecapWorker,
    ReviewWorker,
    SuggestionWorker,
    UpNextWorker,
)


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
        lead: Plans each review: how deep, what to focus on, what goes first
            (a fake in tests).
        suggester: Suggests what you might ask next in a PR's session (a fake in
            tests); by default, ``suggestion_model``'s, if there is one.
        picker: Chooses what's up next in each repo (a fake in tests); by default,
            the lead's model.
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
        lead: LeadAgent | None = None,
        suggester: Suggester | None = None,
        picker: Picker | None = None,
        handle_sigint: bool = True,
    ):
        self._config = config
        self._github = github or GitHub()
        self._instructions = ReviewInstructions(config.config_dir)
        self._rules = RepoRules(config.config_dir)
        self._agent = agent or ClaudeReviewAgent(
            model=config.review_model,
            max_budget_usd=config.review_budget_usd,
            instructions=self._instructions,
            rules=self._rules,
            permission_mode=config.permission_mode,
            sandbox=config.sandbox,
        )
        self._repos = repos or GitRepos(config.data_dir)
        self._writer = writer
        self._recap_agent = recap_agent or ClaudeRecapAgent(model=config.review_model)
        self._lead = lead or ClaudeLead(model=config.lead_model)
        model = config.suggestion_model
        self._suggester = suggester or (ClaudeSuggester(model=model) if model else None)
        self._picker = picker or ClaudePicker(model=config.lead_model)
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
    def instructions(self) -> ReviewInstructions:
        return self._instructions

    @property
    def rules(self) -> RepoRules:
        return self._rules

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
                github=self._github,
                permission_wait=self._config.permission_wait,
                max_reviews=self._config.max_reviews,
                quick_model=self._config.quick_model,
            ),
            LeadWorker(
                store=store,
                agent=self._lead,
                github=self._github,
                repos=self._repos,
                instructions=self._instructions,
                auto_review=self._config.auto_review,
                auto_answer=self._config.auto_answer,
            ),
            RecapWorker(store=store, agent=self._recap_agent),
            SuggestionWorker(store=store, agent=self._agent, suggester=self._suggester),
            GitWorker(store=store, repos=self._repos, github=self._github),
            UpNextWorker(
                store=store,
                github=self._github,
                repos=self._repos,
                picker=self._picker,
                instructions=self._instructions,
                count=self._config.up_next,
            ),
        ]
