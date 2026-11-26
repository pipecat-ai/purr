"""The GitHub worker on a real bus, with GitHub faked out."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.github import PRStatus
from purr.jobs import Invalid, NotFound, request
from purr.messages import (
    DismissAction,
    DismissReplies,
    PRChanged,
    PRListUpdated,
    PurrEvent,
    Synced,
    UnwatchRepo,
    Watching,
    WatchRepo,
)
from purr.models import (
    ActionKind,
    ActionStatus,
    GHState,
    PRChange,
    PRRef,
    PRState,
    Reply,
    Verdict,
)
from purr.store import Store
from purr.workers import ActivityWorker, GitHubWorker
from tests.fakes import FakeGitHub, give_verdict, remote


class Client(BaseWorker):
    def __init__(self):
        super().__init__("client")
        self.events: asyncio.Queue[PurrEvent] = asyncio.Queue()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            await self.events.put(message)

    async def next_event(self, kind: type[PurrEvent]) -> PurrEvent:
        while True:
            event = await asyncio.wait_for(self.events.get(), 5)
            if isinstance(event, kind):
                return event


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 5) -> None:
    async def wait():
        while not await check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


@pytest.fixture
async def setup():
    async with Store.connect(":memory:") as store:
        github = FakeGitHub()
        worker = GitHubWorker(store=store, github=github, interval=3600)
        client = Client()
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(ActivityWorker(store=store), worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            yield store, github, worker, client
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def test_add_poll_and_remove_a_repo(setup):
    store, github, worker, client = setup
    github.open["pipecat-ai/pipecat"] = [remote(1)]

    # Added by URL, stored under GitHub's spelling.
    watching = await request(client, WatchRepo(repo="https://github.com/Pipecat-AI/pipecat"))
    assert watching == Watching(repo="pipecat-ai/pipecat")
    await client.next_event(PRListUpdated)

    # The first sync fills the pool quietly.
    async def synced():
        repo = await store.get_repo("pipecat-ai/pipecat")
        return repo is not None and repo.synced_at is not None

    await eventually(synced)
    assert [pr.number for pr in await store.list_prs()] == [1]

    # Later polls are incremental, report what's new, and it lands in the activity log.
    github.updates["pipecat-ai/pipecat"] = [remote(2)]
    await worker.poll()
    full, incremental = github.calls[-1]
    assert full == [] and list(incremental) == ["pipecat-ai/pipecat"]
    event = await client.next_event(PRChanged)
    assert (event.pr, event.change) == ("pipecat-ai/pipecat#2", PRChange.POOLED)
    await client.next_event(PRListUpdated)
    # Every poll ends saying so (reviews of PRs assigned to you wait for it).
    await client.next_event(Synced)

    async def recorded():
        return len(await store.activity(pr=PRRef("pipecat-ai/pipecat", 2))) == 1

    await eventually(recorded)

    await request(client, UnwatchRepo(repo="pipecat-ai/pipecat"))
    assert await store.list_repos() == []
    assert await store.list_prs() == []


async def test_a_push_the_searches_havent_seen_is_no_news(setup):
    """Right after Purr pushes, the searches can still have the head from before. The PR
    itself has the one pushed: no new commits, nothing to re-review."""
    store, github, worker, client = setup
    await client.next_event(Synced)
    ref = PRRef("pipecat-ai/pipecat", 5)
    github.assigned = [remote(5, head_sha="abc")]
    await worker.poll()
    review = await store.start_review(ref)
    await store.finish_review(review.id, summary="Fine", findings=[])
    await store.set_pr_state(ref, PRState.NEEDS_YOU)
    await store.pushed(ref, "e0b9581")

    github.statuses = {ref: PRStatus(GHState.OPEN, "e0b9581")}
    plan = await worker.poll()
    assert ref in github.status_calls[-1]
    assert plan.changes == []
    assert (await store.get_pr(ref)).head_sha == "e0b9581"

    # The author pushed on top of it: that's news.
    github.assigned = [remote(5, head_sha="f00")]
    github.statuses = {ref: PRStatus(GHState.OPEN, "f00")}
    plan = await worker.poll()
    assert [(c.pr, c.change) for c in plan.changes] == [(ref, PRChange.NEW_COMMITS)]
    assert (await store.get_pr(ref)).head_sha == "f00"


async def test_a_pr_you_merged_stays_merged_while_the_searches_catch_up(setup):
    """Right after you merge a PR, the searches can still list it as open and assigned to
    you (pipecat#6759): it stays merged, and that's no news. Reopened, it's open again."""
    store, github, worker, client = setup
    await client.next_event(Synced)
    ref = PRRef("pipecat-ai/pipecat", 5)
    github.assigned = [remote(5, head_sha="abc")]
    await worker.poll()
    review = await store.start_review(ref)
    await store.finish_review(review.id, summary="Fine", findings=[])
    await store.set_pr_state(ref, PRState.DONE)
    await store.apply_sync(gh_states={ref: GHState.MERGED})  # as merging with m leaves it

    github.statuses = {ref: PRStatus(GHState.MERGED, "abc")}
    plan = await worker.poll()
    assert ref in github.status_calls[-1]
    assert plan.changes == [] and plan.upserts == []
    assert (await store.get_pr(ref)).gh_state == GHState.MERGED

    github.statuses = {ref: PRStatus(GHState.OPEN, "abc")}  # reopened
    await worker.poll()
    assert (await store.get_pr(ref)).gh_state == GHState.OPEN


async def test_adding_an_unknown_repo_is_rejected(setup):
    store, _, _, client = setup
    with pytest.raises(NotFound, match="Can't find nobody/nothing"):
        await request(client, WatchRepo(repo="nobody/nothing"))
    with pytest.raises(Invalid, match="not a GitHub repo"):
        await request(client, WatchRepo(repo="not a repo"))
    with pytest.raises(NotFound, match="isn't being watched"):
        await request(client, UnwatchRepo(repo="nobody/nothing"))
    assert await store.list_repos() == []


async def test_replies_to_you_need_you_until_answered(setup):
    store, github, worker, client = setup
    await client.next_event(Synced)  # the first poll, with nothing reviewed
    ref = PRRef("pipecat-ai/pipecat", 1)
    github.assigned = [remote(1, assignees=("me",))]
    await worker.poll()
    assert (await client.next_event(PRChanged)).change == PRChange.ASSIGNED
    review = await store.start_review(ref)  # reviewed and approved: done
    await store.finish_review(review.id, summary="Fine", findings=[])
    await give_verdict(store, ref)
    await store.set_pr_state(ref, PRState.DONE)

    # Someone answers you: it changed on GitHub, so its conversation is read.
    alice = Reply("alice", "Fixed it.", "2026-11-21T10:00:00Z", "u1", "bus.py:42")
    github.conversations[ref] = [alice]
    github.assigned = [remote(1, assignees=("me",), updated_at="2026-11-21T10:00:00Z")]
    await worker.poll()
    event = await client.next_event(PRChanged)
    assert (event.change, event.by) == (PRChange.REPLIED, "alice")
    pr = await store.get_pr(ref)
    assert (pr.state, pr.replies) == (PRState.NEEDS_YOU, (alice,))

    async def news():
        return await store.unseen() == [(ref, "alice replied")]

    await eventually(news)

    # Nothing changed on GitHub: nothing to read.
    calls = len(github.standing_calls)
    await worker.poll()
    assert len(github.standing_calls) == calls

    # x: no reply needed.
    await request(client, DismissReplies(pr=ref))
    pr = await store.get_pr(ref)
    assert (pr.state, pr.replies) == (PRState.DONE, ())

    # Another one, then you answer it on GitHub.
    bob = Reply("bob", "Why?", "2026-11-21T11:00:00Z")
    github.conversations[ref] = [alice, bob]
    github.assigned = [remote(1, assignees=("me",), updated_at="2026-11-21T11:00:00Z")]
    await worker.poll()
    assert (await store.get_pr(ref)).replies == (bob,)
    assert (await store.get_pr(ref)).state == PRState.NEEDS_YOU
    github.conversations[ref] = []
    github.assigned = [remote(1, assignees=("me",), updated_at="2026-11-21T12:00:00Z")]
    await worker.poll()
    pr = await store.get_pr(ref)
    assert (pr.state, pr.replies) == (PRState.DONE, ())


async def _reviewed(store, github, worker, number: int) -> PRRef:
    """A PR assigned to you, at ``def``, reviewed with no verdict yet: it needs you."""
    ref = PRRef("pipecat-ai/pipecat", number)
    github.assigned = [remote(number, assignees=("me",), head_sha="def")]
    await worker.poll()
    review = await store.start_review(ref)
    await store.finish_review(review.id, summary="Fine", findings=[])
    await store.set_pr_state(ref, PRState.NEEDS_YOU)
    return ref


def _updated(github, number: int, at: str) -> None:
    """The PR changed on GitHub at ``at`` (say, a review), so its conversation is read."""
    github.assigned = [remote(number, assignees=("me",), head_sha="def", updated_at=at)]


async def test_a_verdict_you_gave_on_github_counts(setup):
    store, github, worker, client = setup
    await client.next_event(Synced)
    ref = await _reviewed(store, github, worker, 1)

    # Approved on GitHub, at a commit before: not the PR as it is.
    github.verdicts[ref] = Verdict(ActionKind.APPROVE, "abc")
    _updated(github, 1, "2026-11-21T10:00:00Z")
    await worker.poll()
    pr = await store.get_pr(ref)
    assert (pr.verdict, pr.state) == (Verdict(ActionKind.APPROVE, "abc"), PRState.NEEDS_YOU)
    assert await store.approved_head(ref) == "abc"  # m: new commits since

    # The agent proposes approving it, and you do, on GitHub: done, its proposal moot.
    proposal = await store.add_pending_action(ref, kind=ActionKind.APPROVE, description="LGTM")
    github.verdicts[ref] = Verdict(ActionKind.APPROVE, "def")
    _updated(github, 1, "2026-11-21T11:00:00Z")
    await worker.poll()
    assert (await store.get_pr(ref)).state == PRState.DONE
    assert (await store.get_pending_action(proposal.id)).status == ActionStatus.DISMISSED
    assert await store.approved_head(ref) == "def"  # m can merge it

    # Dismissed on GitHub: no verdict of yours stands (it stays done till something's new).
    github.verdicts.pop(ref)
    _updated(github, 1, "2026-11-21T12:00:00Z")
    await worker.poll()
    assert (await store.get_pr(ref)).verdict is None
    assert await store.approved_head(ref) is None


async def test_a_verdict_on_github_leaves_the_agents_comments_to_you(setup):
    store, github, worker, client = setup
    await client.next_event(Synced)
    ref = await _reviewed(store, github, worker, 2)
    comment = await store.add_pending_action(ref, kind=ActionKind.COMMENT, description="Nit")

    github.verdicts[ref] = Verdict(ActionKind.REQUEST_CHANGES, "def")
    _updated(github, 2, "2026-11-21T10:00:00Z")
    await worker.poll()
    assert (await store.get_pr(ref)).state == PRState.NEEDS_YOU  # the comment waits on you
    assert (await store.get_pending_action(comment.id)).status == ActionStatus.PENDING
    assert await store.approved_head(ref) is None

    await request(client, DismissAction(action_id=comment.id))
    assert (await store.get_pr(ref)).state == PRState.DONE


async def test_replies_are_read_for_every_reviewed_pr_on_startup():
    async with Store.connect(":memory:") as store:
        ref = PRRef("other/repo", 3)
        github = FakeGitHub()
        github.assigned = [remote(3, "other/repo", assignees=("me",))]
        await store.apply_sync(upserts=[github.assigned[0].to_pr("me")])
        await store.set_pr_state(ref, PRState.DONE)
        github.conversations[ref] = [Reply("alice", "Thoughts?", "2026-11-21T10:00:00Z")]
        worker = GitHubWorker(store=store, github=github, interval=3600)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:

            async def read():
                pr = await store.get_pr(ref)
                return pr.state == PRState.NEEDS_YOU and len(pr.replies) == 1

            await eventually(read)
            assert github.standing_calls == [[ref]]
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def test_replies_on_prs_you_took_part_in_on_github(setup):
    store, github, worker, client = setup
    await client.next_event(Synced)
    ref = PRRef("other/repo", 8)
    github.involved = [remote(8, "other/repo")]  # you commented; Purr never reviewed it
    await worker.poll()
    pr = await store.get_pr(ref)
    assert pr.involved and pr.state == PRState.NOT_REVIEWED
    # Quietly: not news, and its repo isn't listed for it.
    assert await store.repo_summaries() == []

    github.conversations[ref] = [Reply("alice", "@me thoughts?", "2026-11-21T10:00:00Z")]
    github.involved = [remote(8, "other/repo", updated_at="2026-11-21T10:00:00Z")]
    await worker.poll()
    event = await client.next_event(PRChanged)
    assert (event.pr, event.change) == ("other/repo#8", PRChange.REPLIED)
    assert (await store.get_pr(ref)).state == PRState.NEEDS_YOU
    [summary] = await store.repo_summaries()
    assert (summary.name, summary.needs_you) == ("other/repo", 1)

    # Answered: back to where it was, not reviewed by Purr.
    await request(client, DismissReplies(pr=ref))
    assert (await store.get_pr(ref)).state == PRState.NOT_REVIEWED
