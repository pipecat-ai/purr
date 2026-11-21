"""The git worker: diffs from the local clone first, GitHub otherwise, and syncing open PRs."""

import asyncio

import pytest
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.github import GitHubError
from purr.messages import GIT_WORKER, PR_DIFF, PRListUpdated
from purr.models import PR, PRRef
from purr.store import Store
from purr.workers import GitWorker
from tests.fakes import FakeGitHub, FakeRepos


def pr(repo: str, number: int) -> PR:
    return PR(repo=repo, number=number, title="t", author="x", url="u", head_sha="h", updated_at="")


class Client(BaseWorker):
    def __init__(self):
        super().__init__("client")

    async def diff(self, ref: PRRef) -> dict:
        params = JobParams(name=PR_DIFF, payload={"pr": str(ref)}, timeout=5)
        async with self.job(GIT_WORKER, params=params) as j:
            pass
        return j.response


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

    assert await client.diff(local.ref) == {"diff": "local diff"}
    assert await client.diff(remote.ref) == {"diff": "github diff"}
    assert github.diff_calls == [remote.ref]

    async def refused(ref):
        raise GitHubError("Sorry, the diff exceeded the maximum number of files (300)")

    github.pr_diff = refused
    assert "maximum number of files" in (await client.diff(remote.ref))["error"]


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
