"""Keeps each repo's clone in step with its open PRs, and serves their diffs from it."""

import asyncio
from collections import defaultdict

from loguru import logger
from pipecat.bus import BusJobRequestMessage, BusMessage
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from purr.git import GitRepos
from purr.github import GitHub, GitHubError
from purr.messages import GIT_WORKER, PR_DIFF, PRListUpdated
from purr.models import PR, PRRef
from purr.store import Store

# Enough for any diff worth reading in a terminal; `d` opens all of it in a pager.
MAX_DIFF_CHARS = 1_000_000


class GitWorker(BaseWorker):
    """Syncs open PRs into the repos' clones, so their diffs are local.

    It syncs on start and whenever the PR list changes (a poll found new or
    updated PRs, mostly), after a short pause so a burst of changes is one
    sync. ``pr.diff`` answers from the clone, and from GitHub for a PR that
    isn't synced yet (a repo still being cloned, a push since the last sync).
    """

    def __init__(
        self,
        name: str = GIT_WORKER,
        *,
        store: Store,
        repos: GitRepos,
        github: GitHub,
        settle: float = 1.0,
    ):
        super().__init__(name)
        self._store = store
        self._repos = repos
        self._github = github
        self._settle = settle
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def on_activated(self, args: dict | None) -> None:
        await super().on_activated(args)
        if self._task is None:
            self._changed.set()  # once at start
            self._task = self.create_task(self._sync_loop(), f"{self}::sync")

    async def stop(self) -> None:
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        await super().stop()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PRListUpdated):
            self._changed.set()

    async def _sync_loop(self) -> None:
        while True:
            await self._changed.wait()
            await asyncio.sleep(self._settle)
            self._changed.clear()
            await self.sync()

    async def sync(self) -> None:
        """Sync every repo with open PRs; one failing doesn't stop the rest."""
        by_repo: dict[str, list[PR]] = defaultdict(list)
        for pr in await self._store.list_prs(open_only=True):
            by_repo[pr.repo].append(pr)
        for repo, prs in by_repo.items():
            try:
                await self._repos.sync(repo, prs)
            except Exception as e:
                logger.warning(f"Couldn't sync {repo}'s PRs into its clone: {e}")

    @job(name=PR_DIFF)
    async def on_pr_diff(self, message: BusJobRequestMessage) -> None:
        try:
            ref = PRRef.parse(str((message.payload or {}).get("pr")))
        except ValueError as e:
            await self.send_job_response(message.job_id, {"error": str(e)})
            return
        pr = await self._store.get_pr(ref)
        diff = await self._repos.diff(pr) if pr else None
        if diff is None:
            try:
                diff = await self._github.pr_diff(ref)
            except GitHubError as e:
                error = f"Couldn't read the diff from GitHub: {e}"
                await self.send_job_response(message.job_id, {"error": error})
                return
        await self.send_job_response(message.job_id, {"diff": diff[:MAX_DIFF_CHARS]})
