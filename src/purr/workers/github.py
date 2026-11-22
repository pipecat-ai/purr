"""The GitHub worker: polls GitHub and manages the watched repos."""

import asyncio
import contextlib
import dataclasses
import time
from datetime import UTC, datetime, timedelta

from loguru import logger
from pipecat.workers.base_worker import BaseWorker

from purr.actions import ActionError, GhWriter, GitHubWriter, execute
from purr.github import GitHub, GitHubError, RemotePR, Snapshot
from purr.jobs import Done, Failed, Invalid, NotFound, handles
from purr.messages import (
    GITHUB_WORKER,
    ActionResolved,
    ApproveAction,
    AssignMe,
    DismissAction,
    DismissReplies,
    FetchDetails,
    FetchedDetails,
    MarkSeen,
    Posted,
    PRChanged,
    PRListUpdated,
    Synced,
    UnwatchRepo,
    Watching,
    WatchRepo,
)
from purr.models import (
    PR,
    ActionStatus,
    GHState,
    PRChange,
    PRRef,
    PRState,
    Reply,
    Repo,
    ReviewStatus,
    parse_repo_name,
)
from purr.store import Store
from purr.sync import Change, SyncPlan, missing_reviewed, plan_sync

# Between full syncs, polls only fetch PRs updated since the last sync. A full
# sync still runs on startup and this often, to catch anything search missed.
FULL_SYNC_EVERY = 15 * 60
# Search indexing lags a little behind GitHub, so incremental windows overlap.
INCREMENTAL_OVERLAP = timedelta(minutes=5)


def _since(synced_at: str) -> str:
    """The ``updated:>=`` bound for an incremental search, in GitHub's format."""
    start = datetime.fromisoformat(synced_at) - INCREMENTAL_OVERLAP
    return start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _changed(before: list[PR], plan: SyncPlan) -> set[PRRef]:
    """The PRs a poll found that are new, or were updated on GitHub since the last one."""
    updated_at = {pr.ref: pr.updated_at for pr in before}
    return {
        pr.ref
        for pr in plan.upserts
        if pr.ref not in updated_at or pr.updated_at != updated_at[pr.ref]
    }


def _replied(old: PR, pr: PR) -> Change | None:
    """News of the replies ``pr`` has that ``old`` didn't, saying who replied."""
    new = [reply for reply in pr.replies if reply not in old.replies]
    if not new:
        return None
    by = ", ".join(dict.fromkeys(reply.author for reply in new))
    return Change(pr.ref, PRChange.REPLIED, pr.title, by=by)


class GitHubWorker(BaseWorker):
    """Keeps the store's PR list in sync with GitHub.

    Polls every ``interval`` seconds (and right away when a repo is added),
    broadcasting a :class:`PRChanged` per change it finds and a
    :class:`PRListUpdated` when anything changed. Handles the ``repo.add`` and
    ``repo.remove`` jobs.

    After a poll it reads the conversation of the PRs Purr reviewed or you took
    part in that changed (all of them on the first poll) for replies to you since
    your last word there: a PR with replies you haven't answered needs you.

    Each repo gets a full sync on its first poll after startup and every
    ``FULL_SYNC_EVERY`` seconds; polls in between only fetch PRs updated since
    the last sync, which is usually a single cheap request. A newly added repo's
    PRs are stored page by page as they arrive, so it shows up quickly.

    Args:
        name: Bus name.
        store: Where PRs and repos live.
        github: GitHub client.
        writer: Posts approved actions to GitHub.
        interval: Seconds between polls.
    """

    def __init__(
        self,
        name: str = GITHUB_WORKER,
        *,
        store: Store,
        github: GitHub,
        writer: GitHubWriter | None = None,
        interval: float = 60.0,
        signature: str = "",
    ):
        super().__init__(name)
        self._signature = signature  # at the end of what Purr posts (see purr.actions)
        self._store = store
        self._github = github
        self._writer = writer or GhWriter()
        self._interval = interval
        self._poll_now = asyncio.Event()
        # Serializes polls with repo changes, so a poll never re-adds PRs from a
        # repo that was just removed.
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self._last_full_sync: dict[str, float] = {}  # repo -> time.monotonic()
        # Reviewed PRs whose replies to read on the next poll; None: all of them.
        self._replies_due: set[PRRef] | None = None

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
            before, plan = await self._sync()
        replied, replies_changed = await self._read_replies(_changed(before, plan))
        for change in [*plan.changes, *replied]:
            await self.send_bus_message(
                PRChanged(
                    source=self.name,
                    pr=str(change.pr),
                    change=change.change.value,
                    title=change.title,
                    by=change.by,
                )
            )
        if not plan.empty or replies_changed:
            await self.send_bus_message(PRListUpdated(source=self.name))
        await self.send_bus_message(Synced(source=self.name))
        return plan

    async def _sync(self) -> tuple[list[PR], SyncPlan]:
        """Bring the store's PR list in line with GitHub. Call with the lock held.

        Returns the open PRs the store had before the plan was applied, and the plan.
        """
        started = datetime.now(UTC).isoformat(timespec="seconds")
        repos = await self._store.list_repos()
        watched = [repo.name for repo in repos]
        first_sync = {repo.name for repo in repos if repo.synced_at is None}
        snapshot = await self._snapshot(repos, first_sync)
        current = await self._store.open_prs()
        missing = missing_reviewed(current, snapshot, watched)
        statuses = await self._github.pr_statuses(missing) if missing else {}
        plan = plan_sync(
            current,
            snapshot,
            watched=watched,
            first_sync=first_sync,
            statuses=statuses,
        )
        await self._store.apply_sync(
            upserts=plan.upserts, deletes=plan.deletes, gh_states=plan.gh_states
        )
        now = time.monotonic()
        for name in snapshot.open:
            self._last_full_sync[name] = now
        await self._store.mark_synced([*snapshot.open, *snapshot.updates], at=started)
        return current, plan

    async def _snapshot(self, repos: list[Repo], first_sync: set[str]) -> Snapshot:
        """Search GitHub: everything in repos due a full sync, what changed in the rest."""
        full = [repo.name for repo in repos if self._needs_full_sync(repo)]
        incremental = {
            repo.name: _since(repo.synced_at)
            for repo in repos
            if repo.name not in full and repo.synced_at
        }

        async def on_page(repo: str, prs: list[RemotePR]) -> None:
            # Show a brand new repo's PRs as they arrive. Upserting them is
            # safe before the poll completes; removals wait for the end.
            if repo not in first_sync:
                return
            me = await self._github.viewer_login()
            await self._store.apply_sync(upserts=[pr.to_pr(me) for pr in prs])
            await self.send_bus_message(PRListUpdated(source=self.name))

        return await self._github.snapshot(full, incremental, on_page=on_page)

    async def _read_replies(self, changed: set[PRRef]) -> tuple[list[Change], bool]:
        """Read what others said to you on PRs Purr reviewed or you took part in, that
        changed (all of them, the first time).

        Returns the PRs with new replies, and whether any PR's replies or state changed.
        """
        followed = {
            pr.ref: pr
            for pr in await self._store.open_prs()
            if pr.state != PRState.NOT_REVIEWED or pr.involved
        }
        due = set(followed) if self._replies_due is None else self._replies_due | changed
        found = await self._fetch_replies(due & followed.keys())
        news, any_changed = [], False
        for ref, replies in found.items():
            old = followed[ref]
            await self._store.set_replies(ref, replies)
            pr = await self._store.get_pr(ref)
            if pr is None or pr.replies == old.replies:
                continue
            any_changed = True
            if change := _replied(old, pr):
                news.append(change)
            await self._needs_you_if_replied(pr)
        return news, any_changed

    async def _fetch_replies(self, due: set[PRRef]) -> dict[PRRef, list[Reply]]:
        """Replies to you on the PRs that are due; any GitHub didn't answer for stay due."""
        if not due:
            self._replies_due = set()
            return {}
        try:
            found = await self._github.replies(sorted(due))
        except GitHubError as e:
            logger.warning(f"Couldn't read replies on {len(due)} PRs: {e}")
            self._replies_due = due  # next time
            return {}
        self._replies_due = due - found.keys()
        return found

    async def _needs_you_if_replied(self, pr: PR) -> None:
        """A PR with replies to you needs you; once none are left, it settles back."""
        if pr.replies and pr.state in (PRState.DONE, PRState.NOT_REVIEWED):
            await self._store.set_pr_state(pr.ref, PRState.NEEDS_YOU)
        elif not pr.replies:
            await self._settle(pr)

    async def _settle(self, pr: PR, *, done: bool = False) -> None:
        """Back where it was, if it needed you and nothing does any more (and no agent is
        at it): done (``done``: you decided something), or not reviewed if only replies
        on GitHub brought a PR Purr never reviewed here."""
        if pr.state != PRState.NEEDS_YOU or pr.gh_state != GHState.OPEN:
            return
        if await self._store.waits_on_you(pr.ref) or await self._store.running(pr.ref):
            return
        review = await self._store.latest_review(pr.ref)
        if review and review.status == ReviewStatus.FAILED:
            return  # it needs you for that
        await self._store.set_pr_state(
            pr.ref, PRState.DONE if review or done else PRState.NOT_REVIEWED
        )

    def _needs_full_sync(self, repo: Repo) -> bool:
        last = self._last_full_sync.get(repo.name)
        return repo.synced_at is None or last is None or time.monotonic() - last > FULL_SYNC_EVERY

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

    @handles(WatchRepo)
    async def watch_repo(self, request: WatchRepo) -> Watching:
        try:
            name = parse_repo_name(request.repo)
        except ValueError as e:
            raise Invalid(str(e)) from e
        try:
            canonical = await self._github.repo_name(name)
        except GitHubError as e:
            raise Failed(str(e)) from e
        if canonical is None:
            raise NotFound(f"Can't find {name} on GitHub (or you don't have access)")
        async with self._lock:
            await self._store.add_repo(canonical)
        await self.send_bus_message(PRListUpdated(source=self.name))
        self.request_poll()
        return Watching(repo=canonical)

    @handles(UnwatchRepo)
    async def unwatch_repo(self, request: UnwatchRepo) -> Done:
        async with self._lock:
            removed = await self._store.remove_repo(request.repo)
        if not removed:
            raise NotFound(f"{request.repo} isn't being watched")
        await self.send_bus_message(PRListUpdated(source=self.name))
        return Done()

    # -- Writes the user asked for -------------------------------------------------

    @handles(MarkSeen)
    async def mark_seen(self, request: MarkSeen) -> Done:
        await self._store.mark_seen(request.pr)
        return Done()

    @handles(DismissReplies)
    async def dismiss_replies(self, request: DismissReplies) -> Done:
        await self._store.dismiss_replies(request.pr)
        if pr := await self._store.get_pr(request.pr):
            await self._settle(pr)
        await self.send_bus_message(PRListUpdated(source=self.name))
        return Done()

    @handles(AssignMe)
    async def assign_me(self, request: AssignMe) -> Done:
        ref = request.pr
        try:
            await self._writer.assign_self(ref)
        except ActionError as e:
            raise Failed(f"Couldn't assign you: {e}") from e
        # Show it right away; the next poll confirms it.
        pr = await self._store.get_pr(ref)
        if pr and not pr.assigned_to_me:
            me = await self._github.viewer_login()
            mine = dataclasses.replace(pr, assigned_to_me=True, assignees=(*pr.assignees, me))
            await self._store.apply_sync(upserts=[mine])
            await self.send_bus_message(PRListUpdated(source=self.name))
        self.request_poll()
        return Done()

    # -- Reads for the UIs ------------------------------------------------------------

    @handles(FetchDetails)
    async def fetch_details(self, request: FetchDetails) -> FetchedDetails:
        try:
            details = await self._github.pr_details(request.prs)
        except GitHubError as e:
            raise Failed(f"Couldn't read the PR from GitHub: {e}") from e
        return FetchedDetails(details={str(ref): d for ref, d in details.items()})

    # -- Approved actions --------------------------------------------------------

    @handles(ApproveAction)
    async def approve_action(self, request: ApproveAction) -> Posted:
        action_id = request.action_id
        # Claiming it first means approving twice (keys and voice at once) posts once.
        action = await self._store.transition_action(
            action_id, expected=ActionStatus.PENDING, new=ActionStatus.APPROVED
        )
        if action is None:
            raise NotFound("That action was already decided, or doesn't exist")
        pr = await self._store.get_pr(action.pr)
        try:
            posted = await execute(
                self._writer,
                action,
                head_sha=pr.head_sha if pr else "",
                signature=self._signature,
            )
        except ActionError as e:
            # Back to pending, so it can be retried.
            await self._store.transition_action(
                action_id, expected=ActionStatus.APPROVED, new=ActionStatus.PENDING
            )
            raise Failed(f"GitHub didn't take it: {e}") from e
        await self._store.transition_action(
            action_id, expected=ActionStatus.APPROVED, new=ActionStatus.EXECUTED
        )
        await self._store.dismiss_replies(action.pr)  # you've had your say
        await self._resolved(action.pr, action_id, approved=True)
        return Posted(url=posted.url, warnings=list(posted.warnings))

    @handles(DismissAction)
    async def dismiss_action(self, request: DismissAction) -> Done:
        action = await self._store.transition_action(
            request.action_id, expected=ActionStatus.PENDING, new=ActionStatus.DISMISSED
        )
        if action is None:
            raise NotFound("That action was already decided, or doesn't exist")
        await self._resolved(action.pr, request.action_id, approved=False)
        return Done()

    async def _resolved(self, ref: PRRef, action_id: str, *, approved: bool) -> None:
        # Nothing left to decide: the PR is done (unless something else needs you).
        if pr := await self._store.get_pr(ref):
            await self._settle(pr, done=True)
        await self.send_bus_message(
            ActionResolved(source=self.name, pr=str(ref), action_id=action_id, approved=approved)
        )
        await self.send_bus_message(PRListUpdated(source=self.name))
