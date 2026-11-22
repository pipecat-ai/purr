"""Keeps each repo's clone in step with its open PRs, serves their diffs from it, and
removes worktrees that are no longer needed."""

import asyncio
from collections import defaultdict

from loguru import logger
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker

from purr.git import GitRepos
from purr.github import GitHub, GitHubError
from purr.jobs import Failed, NotFound, handles
from purr.messages import (
    GIT_WORKER,
    ActionResolved,
    GetDiff,
    GetWorktree,
    PRChanged,
    PRDiff,
    PRListUpdated,
    Synced,
    Worktree,
)
from purr.models import PR, ActionKind, GHState, PRChange, PRRef, PRState
from purr.store import Store

# Enough for any diff worth reading in a terminal; `d` opens all of it in a pager.
MAX_DIFF_CHARS = 1_000_000


class GitWorker(BaseWorker):
    """Syncs open PRs into the repos' clones, so their diffs are local.

    It syncs on start and whenever the PR list changes (a poll found new or
    updated PRs, mostly), after a short pause so a burst of changes is one
    sync. ``GetDiff`` answers from the clone, and from GitHub for a PR that
    isn't synced yet (a repo still being cloned, a push since the last sync).

    A PR's worktree is removed once it's not needed: when you approve the PR
    through Purr, and when it's merged or closed; after the first poll, any
    left from before go too. Not while a review is running in it. Its review
    session survives, and ``GetWorktree`` (or the next review turn) makes the
    worktree again.
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
        self._swept = False

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
        elif isinstance(message, ActionResolved) and message.approved:
            self.create_task(self._approved(message.action_id), f"{self}::cleanup")
        elif isinstance(message, PRChanged) and message.change in (
            PRChange.MERGED,
            PRChange.CLOSED,
        ):
            self.create_task(self.cleanup(PRRef.parse(message.pr)), f"{self}::cleanup")
        elif isinstance(message, Synced) and not self._swept:
            # After the first poll, the store knows what closed while Purr was off.
            self._swept = True
            self.create_task(self.sweep(), f"{self}::sweep")

    async def _approved(self, action_id: str) -> None:
        action = await self._store.get_pending_action(action_id)
        if action and action.kind == ActionKind.APPROVE:
            await self.cleanup(action.pr)

    async def cleanup(self, ref: PRRef) -> bool:
        """Remove the PR's worktree, unless a review is running in it; whether it went."""
        pr = await self._store.get_pr(ref)
        if pr and pr.state == PRState.REVIEWING:
            return False
        try:
            removed = await self._repos.remove_worktree(ref)
        except Exception as e:
            logger.warning(f"Couldn't remove {ref}'s worktree: {e}")
            return False
        if removed:
            logger.info(f"Removed {ref}'s worktree")
        return removed

    async def sweep(self) -> None:
        """Remove the worktrees of PRs that closed, or that you approved."""
        for ref in self._repos.worktrees():
            pr = await self._store.get_pr(ref)
            if pr is None or pr.gh_state != GHState.OPEN or await self._store.approved_by_you(ref):
                await self.cleanup(ref)

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

    @handles(GetWorktree)
    async def get_worktree(self, request: GetWorktree) -> Worktree:
        ref = request.pr
        pr = await self._store.get_pr(ref)
        if pr is None:
            raise NotFound(f"Purr doesn't know {ref}")
        try:
            path = await self._repos.checkout(ref, pr.head_sha)
        except Exception as e:
            raise Failed(f"Couldn't check it out: {e}") from e
        return Worktree(path=str(path))

    @handles(GetDiff)
    async def get_diff(self, request: GetDiff) -> PRDiff:
        ref = request.pr
        pr = await self._store.get_pr(ref)
        diff = await self._repos.diff(pr) if pr else None
        if diff is None:
            try:
                diff = await self._github.pr_diff(ref)
            except GitHubError as e:
                raise Failed(f"Couldn't read the diff from GitHub: {e}") from e
        return PRDiff(diff=diff[:MAX_DIFF_CHARS])
