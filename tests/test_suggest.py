"""What you might ask next: what the suggester reads, and the worker."""

import asyncio

import pytest
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.jobs import request
from purr.messages import SuggestPrompt
from purr.models import PR, ActionKind, PRRef
from purr.review.tools import PROPOSE_ACTION, PURR_TOOL_PREFIX, SUBMIT_REVIEW
from purr.store import Store
from purr.suggest import suggestion_notes
from purr.workers import SuggestionWorker
from tests.fakes import FakeAgent, FakeSuggester

REF = PRRef("pipecat-ai/pipecat", 2841)
PR_ = PR(
    repo=REF.repo,
    number=REF.number,
    title="Add message bus",
    author="someone",
    url="u",
    head_sha="abc",
    updated_at="2026-11-20T10:00:00Z",
)
CONVERSATION = [
    {"kind": "user", "text": "Review it"},
    {"kind": "thinking", "text": "Let me look at the diff"},
    {"kind": "tool_use", "id": "1", "name": "Bash", "input": {"command": "gh pr diff"}},
    {"kind": "tool_result", "id": "1", "text": "diff --git ..."},
    {
        "kind": "tool_use",
        "id": "2",
        "name": PURR_TOOL_PREFIX + SUBMIT_REVIEW,
        "input": {
            "summary": "Clean refactor, one race.",
            "findings": [{"severity": "issue", "text": "Race on shutdown"}],
        },
    },
    {
        "kind": "tool_use",
        "id": "3",
        "name": PURR_TOOL_PREFIX + PROPOSE_ACTION,
        "input": {"description": "Request changes", "comments": [{"path": "a", "line": 1}]},
    },
    {"kind": "text", "text": "Done: one race on shutdown."},
]


def test_notes_say_the_conversation_not_its_tools():
    notes = suggestion_notes(PR_, CONVERSATION, [])
    assert notes.startswith('PR: pipecat-ai/pipecat#2841 "Add message bus" by someone')
    assert "User: Review it" in notes
    assert "Agent's review: Clean refactor, one race. Findings: issue: Race on shutdown" in notes
    assert "Agent proposed: Request changes (1 inline comments)" in notes
    assert notes.endswith("Agent: Done: one race on shutdown.")
    assert "gh pr diff" not in notes and "Let me look" not in notes


def test_notes_keep_the_end_of_a_long_conversation():
    long = [{"kind": "text", "text": f"step {i} " + "x" * 200} for i in range(200)]
    notes = suggestion_notes(PR_, long, [])
    assert "step 199" in notes and "step 0 " not in notes
    assert len(notes) < 13_000


@pytest.fixture
async def setup():
    async with Store.connect(":memory:") as store:
        agent, suggester = FakeAgent(), FakeSuggester()
        client = BaseWorker("client")
        runner = WorkerRunner(handle_sigint=False)
        worker = SuggestionWorker(store=store, agent=agent, suggester=suggester)
        await runner.add_workers(worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))
        try:
            yield store, agent, suggester, client, worker
        finally:
            await runner.end()
            await asyncio.wait_for(task, 5)


async def suggest(client) -> str:
    return (await request(client, SuggestPrompt(pr=REF), timeout=5)).text


async def test_the_worker_suggests_from_the_session(setup):
    store, agent, suggester, client, _ = setup
    await store.upsert_pr(PR_)
    assert await suggest(client) == ""  # no review, nothing to go on

    review = await store.start_review(REF, worktree_path="/tmp/wt")
    await store.set_review_session(review.id, "s1")
    assert await suggest(client) == ""  # nothing said in it yet
    assert suggester.notes == []

    agent.entries["s1"] = CONVERSATION
    await store.add_pending_action(REF, kind=ActionKind.REQUEST_CHANGES, description="Post 1")
    assert await suggest(client) == "Is the race on shutdown real?"
    assert "Waiting for the user's OK: Post 1" in suggester.notes[-1]

    suggester.fail = True  # a suggestion that doesn't come is no suggestion
    assert await suggest(client) == ""


async def test_no_suggester_no_suggestions(setup):
    store, agent, suggester, client, worker = setup
    await store.upsert_pr(PR_)
    review = await store.start_review(REF, worktree_path="/tmp/wt")
    await store.set_review_session(review.id, "s1")
    agent.entries["s1"] = CONVERSATION
    worker._suggester = None  # suggestion_model = ""
    assert await suggest(client) == ""
