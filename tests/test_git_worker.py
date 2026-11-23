"""The git worker: diffs from the local clone first, GitHub otherwise, and syncing open PRs."""

import asyncio

import pytest
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.github import GitHubError
from purr.jobs import Failed, request
from purr.messages import GetDiff, GetFile, GetWorktree, PRListUpdated
from purr.models import PR, PRRef
from purr.store import Store
from purr.workers import GitWorker
from tests.fakes import FakeGitHub, FakeRepos


def pr(repo: str, number: int) -> PR:
    return PR(repo=repo, number=number, title="t", author="x", url="u", head_sha="h", updated_at="")


class Client(BaseWorker):
    def __init__(self):
        super().__init__("client")

    async def diff(self, ref: PRRef) -> str:
        return (await request(self, GetDiff(pr=ref), timeout=5)).diff


@pytest.fixture
async def setup(tmp_path):
    store = await Store.open(":memory:")
    repos, github, client = FakeRepos(tmp_path), FakeGitHub(), Client()
    runner = WorkerRunner(handle_sigint=False)
    worker = GitWorker(store=store, repos=repos, github=github, settle=0)
    await runner.add_workers(worker, client)
    task = asyncio.create_task(runner.run(auto_end=False))
    await asyncio.sleep(0.05)
    yield store, repos, github, client
    await runner.end()
    await asyncio.wait_for(task, 5)
    await store.close()


async def test_diffs_come_from_the_clone_or_else_github(setup):
    store, repos, github, client = setup
    local, remote = pr("a/b", 1), pr("a/b", 2)
    await store.apply_sync(upserts=[local, remote])
    repos.diffs = {local.ref: "local diff"}
    github.diffs = {remote.ref: "github diff"}

    assert await client.diff(local.ref) == "local diff"
    assert await client.diff(remote.ref) == "github diff"
    assert github.diff_calls == [remote.ref]

    async def refused(ref):
        raise GitHubError("Sorry, the diff exceeded the maximum number of files (300)")

    github.pr_diff = refused
    with pytest.raises(Failed, match="maximum number of files"):
        await client.diff(remote.ref)


async def test_files_at_the_prs_head_come_from_the_clone_or_else_github(setup):
    store, repos, github, client = setup
    local, remote = pr("a/b", 1), pr("a/b", 2)
    await store.apply_sync(upserts=[local, remote])
    repos.files = {(local.ref, "src/a.py"): "local\n"}
    github.files = {("a/b", "src/a.py"): "from github\n"}

    async def read(ref: PRRef) -> str:
        return (await request(client, GetFile(pr=ref, path="src/a.py"), timeout=5)).text

    assert await read(local.ref) == "local\n"
    assert await read(remote.ref) == "from github\n"
    github.files = {}
    with pytest.raises(Failed, match="Couldn't read src/a.py"):
        await read(remote.ref)


async def test_open_prs_are_synced_by_repo_when_the_list_changes(setup):
    store, repos, _, client = setup
    await asyncio.sleep(0.05)
    assert getattr(repos, "synced", []) == []  # nothing open yet
    await store.apply_sync(upserts=[pr("a/b", 1), pr("a/b", 2), pr("c/d", 3)])
    await client.send_bus_message(PRListUpdated(source="test"))
    for _ in range(100):
        if getattr(repos, "synced", []):
            break
        await asyncio.sleep(0.01)
    assert sorted((repo, sorted(numbers)) for repo, numbers in repos.synced) == [
        ("a/b", [1, 2]),
        ("c/d", [3]),
    ]


async def until(check, timeout: float = 5) -> None:
    async def wait():
        while not await check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


async def test_worktrees_go_when_approved_merged_or_closed(setup):
    from purr.messages import ActionResolved, PRChanged
    from purr.models import ActionKind, ActionStatus, PRState

    store, repos, _, client = setup
    approved, merged, changes, busy = (pr("a/b", n) for n in (1, 2, 3, 4))
    await store.apply_sync(upserts=[approved, merged, changes, busy])
    for p in (approved, merged, changes, busy):
        await repos.checkout(p.ref, "h")

    async def gone(*prs) -> bool:
        return all(not repos.worktree_path(p.ref).exists() for p in prs)

    # Approved through Purr: gone. Requesting changes: kept, the review goes on.
    for p, kind in ((approved, ActionKind.APPROVE), (changes, ActionKind.REQUEST_CHANGES)):
        action = await store.add_pending_action(p.ref, kind=kind, description="x", payload={})
        await store.transition_action(
            action.id, expected=ActionStatus.PENDING, new=ActionStatus.EXECUTED
        )
        await client.send_bus_message(
            ActionResolved(source="github", pr=str(p.ref), action_id=action.id, approved=True)
        )
    # Merged: gone, unless a review is still running in it.
    await store.set_pr_state(busy.ref, PRState.REVIEWING)
    for p in (merged, busy):
        await client.send_bus_message(
            PRChanged(source="github", pr=str(p.ref), change="merged", title="t")
        )
    await until(lambda: gone(approved, merged))
    await asyncio.sleep(0.05)
    assert repos.worktree_path(changes.ref).exists()
    assert repos.worktree_path(busy.ref).exists()

    # Needed again (Claude Code, say): made again.
    worktree = await request(client, GetWorktree(pr=approved.ref), timeout=5)
    assert worktree.path == str(repos.worktree_path(approved.ref))
    assert repos.worktree_path(approved.ref).exists()


async def test_leftover_worktrees_are_swept_after_the_first_poll(setup):
    from purr.messages import Synced
    from purr.models import ActionKind, ActionStatus, GHState

    store, repos, _, client = setup
    open_, closed, approved = (pr("a/b", n) for n in (1, 2, 3))
    await store.apply_sync(upserts=[open_, closed, approved])
    await store.apply_sync(gh_states={closed.ref: GHState.CLOSED})
    action = await store.add_pending_action(
        approved.ref, kind=ActionKind.APPROVE, description="x", payload={}
    )
    await store.transition_action(
        action.id, expected=ActionStatus.PENDING, new=ActionStatus.EXECUTED
    )
    for p in (open_, closed, approved):
        await repos.checkout(p.ref, "h")
    await repos.checkout(PRRef("gone/repo", 9), "h")  # a PR Purr no longer knows

    await client.send_bus_message(Synced(source="github"))

    async def swept() -> bool:
        return repos.worktrees() == [open_.ref]

    await until(swept)
