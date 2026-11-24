"""The up next worker: the PRs in each watched repo's pool worth your looking at next."""

import asyncio
import time
from datetime import UTC, datetime

from loguru import logger
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker

from purr.git import GitRepos
from purr.github import GitHub
from purr.jobs import Done, handles
from purr.messages import UP_NEXT_WORKER, ClonesSynced, PRListUpdated, SkipUpNext
from purr.models import PR, FileChange, PRRef, UpNext
from purr.review.instructions import ReviewInstructions
from purr.store import Store
from purr.upnext import (
    Known,
    Picker,
    Signals,
    candidates,
    fill,
    notes,
    picks_from,
    shortlist,
    signals,
)

KNOWN_FOR = 6 * 3600  # seconds what you know of a repo is kept before asking GitHub again
ASK_EVERY = 10 * 60  # seconds at least between asking the lead about one repo


class UpNextWorker(BaseWorker):
    """Keeps what's up next in each watched repo (see :mod:`purr.upnext`).

    When the PR list changes and after the clones sync (``git.synced``), for each
    watched repo: the PRs in its pool are sized from the clone and weighed against
    what you know of the repo (your merged and reviewed PRs, from GitHub every few
    hours), and the most promising go to the lead, which chooses ``count`` of them
    and says why. What it leaves (all of it, if it can't say) is filled from the top
    of the shortlist, so up next stays full while the pool has enough.

    It asks again when that shortlist changed: at once when up next is short (you
    took one, or skipped it: ``SkipUpNext {pr}``, ``s``), else not more often than
    ``ask_every``. The lead's told what you skipped and took lately.

    Args:
        name: Bus name.
        store: PRs, and what's up next.
        github: For what you know of each repo.
        repos: The clones, for each PR's size.
        picker: The lead, choosing.
        instructions: Your review style, for the lead; none: without it.
        count: How many are up next in a repo; 0: none.
        ask_every: Seconds at least between asking the lead about one repo.
    """

    def __init__(
        self,
        name: str = UP_NEXT_WORKER,
        *,
        store: Store,
        github: GitHub,
        repos: GitRepos,
        picker: Picker,
        instructions: ReviewInstructions | None = None,
        count: int = 5,
        ask_every: float = ASK_EVERY,
    ):
        super().__init__(name)
        self._store = store
        self._github = github
        self._repos = repos
        self._picker = picker
        self._instructions = instructions
        self._count = count
        self._ask_every = ask_every
        self._known: dict[str, tuple[Known, float]] = {}  # repo -> what you know, and when
        self._changes: dict[tuple[PRRef, str], list[FileChange]] = {}  # by PR and head
        self._asked: dict[str, tuple[tuple, float]] = {}  # repo -> the shortlist asked, when
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def on_activated(self, args: dict | None) -> None:
        await super().on_activated(args)
        if not self._count:
            await self._clear()  # turned off: nothing left from before
        elif self._task is None:
            self._task = self.create_task(self._loop(), f"{self}::pick")

    async def stop(self) -> None:
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        await super().stop()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, ClonesSynced):
            self._wake.set()
        elif isinstance(message, PRListUpdated) and message.source != self.name:
            self._wake.set()  # one taken, say: its place is filled

    @handles(SkipUpNext)
    async def skip(self, request: SkipUpNext) -> Done:
        await self._store.skip_up_next(request.pr)
        self._wake.set()  # one fewer: its place is filled
        await self.send_bus_message(PRListUpdated(source=self.name))
        return Done()

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.refresh()
            except Exception:
                logger.exception("Couldn't work out what's up next")

    async def refresh(self) -> None:
        """What's up next in every watched repo, where it may have changed."""
        changed = [await self.pick(repo.name) for repo in await self._store.list_repos()]
        if any(changed):
            await self.send_bus_message(PRListUpdated(source=self.name))

    async def pick(self, repo: str) -> bool:
        """Work out what's up next in ``repo``, unless its shortlist is as when the lead was
        last asked, or (while up next is full) that was too recently. Returns whether
        what's up next changed."""
        short = await self._shortlist(repo)
        asked = self._asked.get(repo)
        key = tuple((s.pr.ref, s.pr.head_sha) for s in short)
        full = len(await self._store.up_next(repo)) >= min(self._count, len(short))
        recently = asked and time.monotonic() - asked[1] < self._ask_every
        if asked and (asked[0] == key or (full and recently)):
            return False
        self._asked[repo] = (key, time.monotonic())
        picks = await self._choose(repo, short) if short else []
        if picks == await self._store.up_next(repo):
            return False  # nothing to draw again
        await self._store.set_up_next(repo, picks)
        return True

    async def _shortlist(self, repo: str) -> list[Signals]:
        """The repo's pool, the most worth looking at first; PRs the clone can't size yet
        are left out."""
        skipped = {pr.number for pr in await self._store.skipped(repo, limit=1000)}
        listed = await self._store.list_prs(repo=repo, open_only=True)
        prs = [pr for pr in candidates(listed) if pr.number not in skipped]
        self._forget(repo, {(pr.ref, pr.head_sha) for pr in prs})
        known, now = await self._known_of(repo), datetime.now(UTC)
        sized = [(pr, await self._changes_of(pr)) for pr in prs]
        return shortlist(signals(pr, changes, known, now) for pr, changes in sized if changes)

    async def _choose(self, repo: str, short: list[Signals]) -> list[UpNext]:
        """The lead's choice from the shortlist, filled from its top to ``count``."""
        told = notes(
            repo,
            short,
            instructions=self._instructions.for_repo(repo) if self._instructions else "",
            taken=await self._store.taken(repo),
            skipped=await self._store.skipped(repo),
        )
        try:
            picks = picks_from(await self._picker.pick(told, self._count), short, self._count)
        except Exception as e:
            logger.warning(f"The lead couldn't say what's up next in {repo}: {e}")
            picks = []
        return fill(picks, short, self._count)

    async def _known_of(self, repo: str) -> Known:
        """What you know of the repo, asked of GitHub every few hours (none, if it can't say)."""
        kept = self._known.get(repo)
        if kept and time.monotonic() - kept[1] < KNOWN_FOR:
            return kept[0]
        try:
            known = Known.of(await self._github.history(repo))
        except Exception as e:
            logger.warning(f"Couldn't read what you know of {repo}: {e}")
            return kept[0] if kept else Known()  # and ask again next time
        self._known[repo] = (known, time.monotonic())
        return known

    async def _changes_of(self, pr: PR) -> list[FileChange] | None:
        """The PR's files with their lines, from the clone, kept by head; None until synced."""
        key = (pr.ref, pr.head_sha)
        if key not in self._changes:
            changes = await self._repos.numstat(pr)
            if changes is None:
                return None
            self._changes[key] = changes
        return self._changes[key]

    def _forget(self, repo: str, current: set[tuple[PRRef, str]]) -> None:
        """Let go of the repo's PRs that left the pool, or moved on."""
        gone = [key for key in self._changes if key[0].repo == repo and key not in current]
        for key in gone:
            del self._changes[key]

    async def _clear(self) -> None:
        for repo in await self._store.list_repos():
            await self._store.set_up_next(repo.name, [])
