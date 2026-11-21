"""The GitHub worker: polls GitHub and manages the watched repos."""

import asyncio
import contextlib

from loguru import logger
from pipecat.bus import BusJobRequestMessage
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from purr.github import GitHub, GitHubError
from purr.messages import GITHUB_WORKER, REPO_ADD, REPO_REMOVE, PRChanged, PRListUpdated
from purr.models import parse_repo_name
from purr.store import Store
from purr.sync import SyncPlan, missing_reviewed, plan_sync


class GitHubWorker(BaseWorker):
    """Keeps the store's PR list in sync with GitHub.

    Polls every ``interval`` seconds (and right away when a repo is added),
    broadcasting a :class:`PRChanged` per change it finds and a
    :class:`PRListUpdated` when anything changed. Handles the ``repo.add`` and
    ``repo.remove`` jobs.

    Args:
        name: Bus name.
        store: Where PRs and repos live.
        github: GitHub client.
        interval: Seconds between polls.
    """

    def __init__(
        self,
        name: str = GITHUB_WORKER,
        *,
        store: Store,
        github: GitHub,
        interval: float = 60.0,
    ):
        super().__init__(name)
        self._store = store
        self._github = github
        self._interval = interval
        self._poll_now = asyncio.Event()
        # Serializes polls with repo changes, so a poll never re-adds PRs from a
        # repo that was just removed.
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None

    async def on_activated(self, args: dict | None) -> None:
        await super().on_activated(args)
        if self._poll_task is None:
            self._poll_task = self.create_task(self._poll_loop(), f"{self}::poll")

    async def stop(self) -> None:
        if self._poll_task:
            await self.cancel_task(self._poll_task)
            self._poll_task = None
        await super().stop()

    def request_poll(self) -> None:
        """Poll as soon as possible instead of waiting for the next interval."""
        self._poll_now.set()

    async def poll(self) -> SyncPlan:
        """Fetch from GitHub, update the store and broadcast what changed."""
        async with self._lock:
            repos = await self._store.list_repos()
            watched = [repo.name for repo in repos]
            snapshot = await self._github.snapshot(watched)
            current = await self._store.open_prs()
            missing = missing_reviewed(current, snapshot, watched)
            statuses = await self._github.pr_statuses(missing) if missing else {}
            plan = plan_sync(
                current,
                snapshot,
                watched=watched,
                first_sync=[repo.name for repo in repos if repo.synced_at is None],
                statuses=statuses,
            )
            await self._store.apply_sync(
                upserts=plan.upserts, deletes=plan.deletes, gh_states=plan.gh_states
            )
            await self._store.mark_synced(name for name in watched if name in snapshot.pool)

        for change in plan.changes:
            await self.send_bus_message(
                PRChanged(
                    source=self.name,
                    pr=str(change.pr),
                    change=change.change.value,
                    title=change.title,
                )
            )
        if not plan.empty:
            await self.send_bus_message(PRListUpdated(source=self.name))
        return plan

    async def _poll_loop(self) -> None:
        while True:
            self._poll_now.clear()
            try:
                await self.poll()
            except GitHubError as e:
                logger.warning(f"GitHub poll failed: {e}")
            except Exception:
                logger.exception("GitHub poll failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._poll_now.wait(), self._interval)

    async def _fail(self, message: BusJobRequestMessage, error: str) -> None:
        await self.send_job_response(message.job_id, {"error": error}, status=JobStatus.FAILED)

    @job(name=REPO_ADD)
    async def on_repo_add(self, message: BusJobRequestMessage) -> None:
        try:
            name = parse_repo_name(str((message.payload or {}).get("repo", "")))
            canonical = await self._github.repo_name(name)
        except (ValueError, GitHubError) as e:
            await self._fail(message, str(e))
            return
        if canonical is None:
            await self._fail(message, f"Can't find {name} on GitHub (or you don't have access)")
            return
        async with self._lock:
            await self._store.add_repo(canonical)
        await self.send_job_response(message.job_id, {"repo": canonical})
        await self.send_bus_message(PRListUpdated(source=self.name))
        self.request_poll()

    @job(name=REPO_REMOVE)
    async def on_repo_remove(self, message: BusJobRequestMessage) -> None:
        name = str((message.payload or {}).get("repo", ""))
        async with self._lock:
            removed = await self._store.remove_repo(name)
        if not removed:
            await self._fail(message, f"{name} isn't being watched")
            return
        await self.send_job_response(message.job_id, {"repo": name})
        await self.send_bus_message(PRListUpdated(source=self.name))
