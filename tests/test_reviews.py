"""The review worker on a real bus, with a scripted agent."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pipecat.bus import BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.messages import (
    LEAD_REVIEW,
    LEAD_WORKER,
    PERMISSION_ANSWER,
    REPO_SKILLS,
    REVIEW_MESSAGE,
    REVIEW_MODEL,
    REVIEW_SKILL,
    REVIEW_SKILLS,
    REVIEW_START,
    REVIEW_WORKER,
    ActionPending,
    ActionResolved,
    PermissionRequest,
    PermissionResolved,
    PRChanged,
    PurrEvent,
    ReviewFailed,
    ReviewFinished,
    ReviewProgress,
    ReviewStarted,
    SessionMessage,
    SkillChosen,
    SkillQuestion,
    Synced,
)
from purr.models import PR, ActionStatus, PRRef, PRState, ReviewStatus
from purr.review.agent import RESUME_PROMPT
from purr.store import Store
from purr.workers import ActivityWorker, LeadWorker, ReviewWorker
from tests.fakes import FakeAgent, FakeLead, FakeRepos, make_plugin

REF = PRRef("pipecat-ai/pipecat", 2841)


def make_pr(**kwargs) -> PR:
    kwargs.setdefault("head_sha", "abc123")
    kwargs.setdefault("number", REF.number)
    return PR(
        repo=REF.repo,
        title="Add message bus",
        author="someone",
        url="https://github.com/pipecat-ai/pipecat/pull/2841",
        updated_at="2026-11-20T10:00:00Z",
        **kwargs,
    )


class Client(BaseWorker):
    def __init__(self):
        super().__init__("client")
        self.events: list[PurrEvent] = []

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if isinstance(message, PurrEvent):
            self.events.append(message)

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    async def request(self, name: str, payload: dict, worker: str = REVIEW_WORKER) -> dict:
        params = JobParams(name=name, payload=payload, timeout=5)
        async with self.job(worker, params=params) as j:
            pass
        return j.response


async def eventually(check: Callable[[], Awaitable[bool] | bool], timeout: float = 5) -> None:
    async def wait():
        while True:
            result = check()
            if await result if asyncio.iscoroutine(result) else result:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


class Setup:
    def __init__(self, store, agent, repos, client, runner, lead):
        self.store, self.agent, self.repos, self.client, self.runner = (
            store,
            agent,
            repos,
            client,
            runner,
        )
        self.lead = lead

    async def state(self) -> PRState:
        return (await self.store.get_pr(REF)).state


@pytest.fixture
async def make(tmp_path):
    """Builds a running setup; ``before_start`` can seed the store first."""
    cleanups = []

    async def build(
        before_start=None, auto_review=True, permission_wait=300, max_reviews=3
    ) -> Setup:
        store = await Store.open(":memory:")
        agent, repos, client, lead = FakeAgent(), FakeRepos(tmp_path), Client(), FakeLead()
        if before_start:
            await before_start(store, agent)
        worker = ReviewWorker(
            store=store,
            repos=repos,
            agent=agent,
            permission_wait=permission_wait,
            max_reviews=max_reviews,
            quick_model="quick-model",
        )
        # The lead decides what gets reviewed (assigned PRs, new commits).
        lead_worker = LeadWorker(
            store=store, agent=lead, repos=repos, auto_review=auto_review, settle=0.01
        )
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(ActivityWorker(store=store), worker, lead_worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))

        async def cleanup():
            await runner.end()
            await asyncio.wait_for(task, 5)
            await store.close()

        cleanups.append(cleanup)
        await asyncio.sleep(0.05)
        return Setup(store, agent, repos, client, runner, lead)

    yield build
    for cleanup in cleanups:
        await cleanup()


async def test_a_review_produces_findings_and_a_pending_action(make):
    s = await make()
    await s.store.upsert_pr(make_pr())

    response = await s.client.request(REVIEW_START, {"pr": str(REF)})
    assert "review_id" in response
    await eventually(lambda: s.client.of(ReviewFinished))

    review = await s.store.latest_review(REF)
    assert review.status == ReviewStatus.FINISHED
    assert review.summary == "Clean refactor, one race."
    assert review.worktree_path == str(s.repos.worktree_path(REF))
    assert [(f.file, f.line) for f in await s.store.findings(review.id)] == [("bus.py", 42)]
    [action] = await s.store.pending_actions(REF)
    assert action.payload["comments"][0]["body"] == "Race here"
    await eventually(lambda: s.state())
    assert await s.state() == PRState.NEEDS_YOU

    assert s.client.of(ReviewStarted) and s.client.of(ActionPending) and s.client.of(SessionMessage)
    assert s.repos.checkouts == [(REF, "abc123")]
    assert s.agent.runs[0]["resume"] is False
    assert s.agent.runs[0]["session_id"] == review.session_id

    # Starting again supersedes the pending action, and re-reviews.
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    assert (await s.store.get_pending_action(action.id)).status == ActionStatus.DISMISSED
    assert s.client.of(ActionResolved)


async def test_prs_assigned_to_you_are_reviewed_automatically(make):
    s = await make()
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.send_bus_message(
        PRChanged(source="github", pr=str(REF), change="assigned", title="Add message bus")
    )
    await eventually(lambda: s.client.of(ReviewFinished))


async def test_already_assigned_prs_are_reviewed_once_synced(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr(assigned_to_me=True))

    s = await make(before_start=seed)
    # Not from what the store remembers: the PR may have closed meanwhile.
    await asyncio.sleep(0.1)
    assert s.agent.runs == []
    # Once a poll has brought the store up to date, it's reviewed.
    await s.client.send_bus_message(Synced(source="github"))
    await eventually(lambda: s.client.of(ReviewFinished))
    # Later polls don't review it again.
    await s.client.send_bus_message(Synced(source="github"))
    await asyncio.sleep(0.1)
    assert len(s.agent.runs) == 1


async def test_no_auto_review_when_turned_off(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr(assigned_to_me=True))

    s = await make(before_start=seed, auto_review=False)
    await s.client.send_bus_message(Synced(source="github"))
    await asyncio.sleep(0.1)
    assert s.agent.runs == []


async def test_a_failed_review_needs_you(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.fail_with = "out of credits"

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFailed))
    review = await s.store.latest_review(REF)
    assert review.status == ReviewStatus.FAILED and review.error == "out of credits"
    await eventually(lambda: s.state())
    assert await s.state() == PRState.NEEDS_YOU


async def test_one_review_at_a_time_per_pr(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.gate = asyncio.Event()

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    response = await s.client.request(REVIEW_START, {"pr": str(REF)})
    assert "already being reviewed" in response["error"]
    # What you write while it works goes to it, as in Claude Code; commands wait.
    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "why?"})
    assert response == {"ok": True, "while_working": True}
    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "/compact"})
    assert "still working" in response["error"]
    s.agent.gate.set()
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.read == ["why?"] and len(s.agent.runs) == 1


async def test_what_the_agent_didnt_read_in_its_run_comes_after(make):
    """Written as it finished, or to a Claude Code that can't take it mid-run."""
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.gate, s.agent.reads_inbox = asyncio.Event(), False
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.agent.runs)
    await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "and the tests?"})
    s.agent.gate.set()
    await eventually(lambda: len(s.agent.runs) == 2)
    assert s.agent.runs[1]["prompt"].endswith("and the tests?")


async def test_follow_up_continues_the_session(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))

    [proposal] = await s.store.pending_actions(REF)
    s.agent.gate = asyncio.Event()
    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "Is the race real?"})
    assert response == {"ok": True, "set_aside": [proposal.description]}
    await eventually(lambda: len(s.agent.runs) == 2)
    followup = s.agent.runs[1]
    assert followup["resume"] is True and followup["session_id"] == s.agent.runs[0]["session_id"]
    # Writing instead of approving sets the proposal aside, and the agent's told so.
    assert followup["prompt"].endswith("\n\nIs the race real?")
    assert "set aside" in followup["prompt"] and proposal.description in followup["prompt"]
    assert not await s.store.pending_actions(REF)
    assert s.client.of(ActionResolved)[-1].action_id == proposal.id
    # It proposes again (the fake always does), and the PR needs you again.
    s.agent.gate.set()
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    assert len(await s.store.pending_actions(REF)) == 1
    assert await s.state() == PRState.NEEDS_YOU

    # A slash command isn't a reply: the proposal stays.
    await eventually(lambda: REF not in s.runner.get_worker(REVIEW_WORKER).running)
    await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "/compact"})
    await eventually(lambda: len(s.agent.runs) == 3)
    assert s.agent.runs[2]["prompt"] == "/compact"


async def test_interrupted_reviews_resume_on_startup(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr())
        review = await store.start_review(REF)
        await store.set_review_session(review.id, "11111111-1111-1111-1111-111111111111")
        await store.set_pr_state(REF, PRState.REVIEWING)
        agent.sessions.add("11111111-1111-1111-1111-111111111111")

    s = await make(before_start=seed)
    await eventually(lambda: s.client.of(ReviewFinished))
    run = s.agent.runs[0]
    assert run["resume"] is True and run["prompt"] == RESUME_PROMPT


async def test_stopping_leaves_the_review_to_resume(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.gate = asyncio.Event()
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.agent.runs)

    await s.runner.end()
    await asyncio.sleep(0.1)
    assert (await s.store.latest_review(REF)).status == ReviewStatus.RUNNING


async def test_the_agent_waits_for_permission(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.ask = ("Bash", {"command": "uv run pytest"}, "only read-only commands are pre-approved")

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(PermissionRequest))
    [request] = s.client.of(PermissionRequest)
    assert request.description == "$ uv run pytest"
    assert await s.state() == PRState.NEEDS_YOU
    assert not s.client.of(ReviewFinished)

    answer = {"request_id": request.request_id, "allow": True}
    assert await s.client.request(PERMISSION_ANSWER, answer) == {"ok": True, "rule": None}
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.answers == [True]
    assert "error" in await s.client.request(PERMISSION_ANSWER, answer)  # already answered


async def test_writing_when_asked_for_permission_says_no_and_what_to_do(make):
    """As in Claude Code: "No, and tell Claude what to do differently"."""
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.ask = ("Bash", {"command": "uv run pytest"}, "not pre-approved")
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(PermissionRequest))

    text = "Only run tests/test_bus.py"
    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": text})
    assert response == {"ok": True, "answered": True}
    await eventually(lambda: s.client.of(ReviewFinished))
    assert (s.agent.answers, s.agent.insteads) == ([False], [text])
    assert len(s.agent.runs) == 1  # the same run carries on: no follow-up


def test_a_no_with_what_to_do_instead_reaches_the_agent_and_the_transcript():
    import io
    from types import SimpleNamespace

    from rich.console import Console

    from purr.review.agent import (
        PermissionAnswer,
        history_entries,
        permission_result,
        set_aside_note,
    )
    from purr.tui.session import render_entry

    result = permission_result(PermissionAnswer(False, instead="Only bus tests"), [], "a/b", None)
    assert result.message.endswith("Only bus tests") and not result.interrupt

    def stored(type_, content):
        return SimpleNamespace(type=type_, message={"role": type_, "content": content})

    # Reloaded, the thread says what writing decided, and shows what you wrote as you
    # wrote it, without Purr's note to the agent.
    asked = set_aside_note(["Approve"]) + "Is the race real?"
    assert history_entries([stored("user", asked)]) == [
        {"kind": "set_aside", "text": "Approve"},
        {"kind": "user", "text": "Is the race real?"},
    ]
    denied = {"type": "tool_result", "tool_use_id": "t1", "content": result.message}
    [entry] = history_entries([stored("user", [denied])])
    console = Console(width=80, record=True, file=io.StringIO())
    console.print(render_entry(entry))
    shown = console.export_text()
    assert "⎿ Not allowed: you wrote instead" in shown and "> Only bus tests" in shown
    console.print(render_entry({"kind": "set_aside", "text": "Approve"}))
    assert "◼ Proposal set aside: Approve" in console.export_text()


async def test_new_commits_get_a_rereview_in_the_same_session(make):
    s = await make()
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    first = s.agent.runs[0]
    assert (await s.store.latest_review(REF)).head_sha == "abc123"

    # The author pushes; the poller notices.
    await s.store.upsert_pr(make_pr(assigned_to_me=True, head_sha="def456"))
    await s.client.send_bus_message(
        PRChanged(source="github", pr=str(REF), change="new_commits", title="Add message bus")
    )
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    rereview = s.agent.runs[1]
    assert rereview["resume"] is True and rereview["session_id"] == first["session_id"]
    assert "git diff abc123 def456" in rereview["prompt"]
    assert (await s.store.latest_review(REF)).head_sha == "def456"
    assert s.repos.checkouts[-1] == (REF, "def456")

    # Same head again: nothing to re-review.
    worker = s.runner.get_worker(REVIEW_WORKER)
    assert await worker.rereview(REF) is False


async def test_an_unanswered_permission_request_lets_the_agent_carry_on(make):
    s = await make(permission_wait=0.1)
    await s.store.upsert_pr(make_pr())
    s.agent.ask = ("Bash", {"command": "uv --version"}, "not pre-approved")

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.answers == [None]  # "nobody answered", not "no"
    [resolved] = s.client.of(PermissionResolved)
    assert resolved.answered is False and resolved.description == "$ uv --version"


async def test_a_pr_left_reviewing_by_a_cut_short_follow_up_is_settled(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr())
        review = await store.start_review(REF)
        await store.finish_review(review.id, summary="ok", findings=[])
        await store.set_pr_state(REF, PRState.REVIEWING)

    s = await make(before_start=seed)
    await eventually(lambda: s.state())
    for _ in range(100):
        if await s.state() == PRState.DONE:
            break
        await asyncio.sleep(0.01)
    assert await s.state() == PRState.DONE


async def test_reviews_get_the_repo_guidelines(make):
    s = await make()
    s.repos.repo_guidelines = "### AGENTS.md\n\nUse Google-style docstrings."
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert "Google-style" in s.agent.runs[0]["guidelines"]


async def test_reviews_get_the_repo_plugin(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = tmp_path / "plugin"
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["plugin"] == tmp_path / "plugin"


async def test_reviews_go_ahead_without_the_repo_plugin(make):
    s = await make()
    s.repos.plugin_error = OSError("disk full")
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["plugin"] is None


async def test_reviews_wait_in_line_but_your_messages_dont(make):
    s = await make(max_reviews=1)
    first, second, chat = (PRRef("pipecat-ai/pipecat", n) for n in (1, 2, 3))
    for ref in (first, second, chat):
        await s.store.upsert_pr(make_pr(number=ref.number))
    # A PR you've already reviewed, to talk to later.
    await s.client.request(REVIEW_START, {"pr": str(chat)})
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 1)

    s.agent.gate = asyncio.Event()
    await s.client.request(REVIEW_START, {"pr": str(first)})
    await s.client.request(REVIEW_START, {"pr": str(second)})
    await eventually(lambda: [r["pr"] for r in s.agent.runs][-1:] == [first])

    def progress(ref: PRRef) -> list[str]:
        return [e.text for e in s.client.of(ReviewProgress) if e.pr == str(ref)]

    await eventually(lambda: "Queued, next up" in progress(second))
    assert second not in [r["pr"] for r in s.agent.runs]

    # Your message doesn't wait behind them.
    await s.client.request(REVIEW_MESSAGE, {"pr": str(chat), "text": "and the tests?"})
    await eventually(lambda: s.agent.runs[-1]["prompt"].endswith("and the tests?"))
    assert second not in [r["pr"] for r in s.agent.runs]

    # When the first is done, the second goes.
    s.agent.gate.set()
    await eventually(lambda: second in [r["pr"] for r in s.agent.runs])
    await eventually(lambda: any(e.pr == str(second) for e in s.client.of(ReviewFinished)))


async def test_conversations_are_recorded_with_times(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.ask = ("Bash", {"command": "uv run pytest"}, "not pre-approved")

    async def answer_when_asked():
        await eventually(lambda: s.client.of(PermissionRequest))
        request = s.client.of(PermissionRequest)[0]
        await s.client.request(PERMISSION_ANSWER, {"request_id": request.request_id, "allow": True})

    await asyncio.gather(s.client.request(REVIEW_START, {"pr": str(REF)}), answer_when_asked())
    await eventually(lambda: s.client.of(ReviewFinished))
    s.agent.ask = None
    await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "is it safe?"})
    await eventually(lambda: s.agent.runs[-1]["prompt"].endswith("is it safe?"))

    async def two_runs_done():
        runs = await s.store.runs(REF)
        return len(runs) == 2 and all(r["status"] == "finished" for r in runs)

    await eventually(two_runs_done)
    review, message = await s.store.runs(REF)
    assert (review["kind"], message["kind"]) == ("review", "message")
    assert review["session_id"] and review["session_id"] == message["session_id"]
    assert review["queued_at"] <= review["started_at"] <= review["finished_at"]
    assert (review["model"], review["cost_usd"], review["turns"]) == ("claude-opus-5-5", 0.25, 4)
    assert (review["input_tokens"], review["output_tokens"], review["cache_read_tokens"]) == (
        10,
        20,
        300,
    )

    messages = await s.store.messages(REF)
    timeline = [(m["run_id"], m["kind"], m["author"]) for m in messages]
    assert timeline[:4] == [
        (review["id"], "prompt", "purr"),
        (review["id"], "permission_request", "agent"),
        (review["id"], "permission_answer", "you"),
        (review["id"], "tool_use", "agent"),
    ]
    assert (message["id"], "prompt", "you") in timeline
    assert messages[1]["data"]["description"] == "$ uv run pytest"
    assert messages[2]["data"] == {
        "kind": "permission_answer",
        "allowed": True,
        "answered": True,
        "remember": None,
    }
    assert all(m["at"] for m in messages)
    assert [m["at"] for m in messages] == sorted(m["at"] for m in messages)


async def test_failed_and_interrupted_runs_are_recorded(make):
    async def seed(store, agent):
        await store.upsert_pr(make_pr())
        await store.start_run(REF, kind="review", review_id=None, session_id=None)

    s = await make(before_start=seed)
    await eventually(lambda: _statuses(s, ["interrupted"]))
    s.agent.fail_with = "API overloaded"
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: _statuses(s, ["interrupted", "failed"]))
    failed = (await s.store.runs(REF))[1]
    assert failed["error"] == "API overloaded"
    assert (await s.store.messages(REF))[-1]["data"] == {"kind": "error", "text": "API overloaded"}


async def _statuses(s, expected: list[str]) -> bool:
    return [r["status"] for r in await s.store.runs(REF)] == expected


# -- The repo's own review skill ------------------------------------------------------

CODE_REVIEW = {"code-review": "Automated code review for pull requests"}
UNCLEAR = {"prose-review": "Check the prose", "security-review": "Look for security problems"}


async def test_a_clear_review_skill_is_followed_without_asking(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", CODE_REVIEW)
    await s.store.upsert_pr(make_pr())

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    [run] = s.agent.runs
    assert run["prompt"] == "/pipecat:code-review https://github.com/pipecat-ai/pipecat/pull/2841"
    assert run["skill"] == "pipecat:code-review"
    assert not s.client.of(SkillQuestion)
    [chosen] = s.client.of(SkillChosen)
    assert (chosen.skill, chosen.chosen_by) == ("pipecat:code-review", "purr")
    assert await s.store.review_skill(REF.repo) == "pipecat:code-review"
    assert (await s.store.latest_review(REF)).skill == "pipecat:code-review"

    # Re-reviews follow it again; it's chosen once.
    await s.store.upsert_pr(make_pr(assigned_to_me=True, head_sha="def456"))
    await s.runner.get_worker(REVIEW_WORKER).rereview(REF)
    await eventually(lambda: len(s.agent.runs) == 2)
    assert s.agent.runs[1]["skill"] == "pipecat:code-review"
    assert "pipecat:code-review" in s.agent.runs[1]["prompt"]
    assert len(s.client.of(SkillChosen)) == 1


async def test_you_are_asked_when_its_not_clear_and_the_review_waits(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", UNCLEAR)
    await s.store.upsert_pr(make_pr())

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(SkillQuestion))
    [question] = s.client.of(SkillQuestion)
    assert question.repo == REF.repo
    assert [skill["name"] for skill in question.skills] == [
        "pipecat:prose-review",
        "pipecat:security-review",
    ]
    await asyncio.sleep(0.05)
    assert not s.agent.runs  # waiting for you

    await s.client.request(REVIEW_SKILL, {"repo": REF.repo, "skill": "pipecat:security-review"})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] == "pipecat:security-review"
    assert await s.store.review_skill(REF.repo) == "pipecat:security-review"
    assert s.client.of(SkillChosen)[0].chosen_by == "you"


async def test_reviews_go_ahead_if_nobody_answers_about_the_skill(make, tmp_path):
    s = await make(permission_wait=0.1)
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", UNCLEAR)
    other = PRRef(REF.repo, 7)
    await s.store.upsert_pr(make_pr())
    await s.store.upsert_pr(make_pr(number=7))

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] is None
    assert s.agent.runs[0]["prompt"].startswith("Review pipecat-ai/pipecat#2841")
    assert await s.store.review_skill(REF.repo) is None  # still to decide, next time

    # Asked once per run: the next review doesn't wait again.
    await s.client.request(REVIEW_START, {"pr": str(other)})
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    assert len(s.client.of(SkillQuestion)) == 1


async def test_not_now_lets_waiting_reviews_go_ahead(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", UNCLEAR)
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(SkillQuestion))

    await s.client.request(REVIEW_SKILL, {"repo": REF.repo, "skill": None})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] is None
    assert await s.store.review_skill(REF.repo) is None
    assert not s.client.of(SkillChosen)


async def test_your_choice_sticks(make, tmp_path):
    async def seed(store, agent):
        await store.set_review_skill(REF.repo, "", chosen_by="you")  # Purr's own review

    s = await make(before_start=seed)
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", CODE_REVIEW)
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] is None
    assert not s.client.of(SkillQuestion) and not s.client.of(SkillChosen)

    response = await s.client.request(REVIEW_SKILLS, {"repo": REF.repo})
    assert response["chosen"] == ""
    assert [skill["name"] for skill in response["skills"]] == ["pipecat:code-review"]


async def test_a_chosen_skill_the_repo_dropped_means_purrs_own_review(make, tmp_path):
    async def seed(store, agent):
        await store.set_review_skill(REF.repo, "pipecat:old-review", chosen_by="you")

    s = await make(before_start=seed)
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", CODE_REVIEW)
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] is None


# -- The lead -----------------------------------------------------------------------


async def test_the_lead_plans_assigned_prs_together_and_they_run_by_priority(make):
    async def seed(store, agent):
        for number in (1, 2, 3):
            await store.upsert_pr(make_pr(number=number, assigned_to_me=True))
        agent.gate = asyncio.Event()  # hold the first review, so the rest wait in line

    s = await make(before_start=seed, max_reviews=1)
    s.lead.plans = {
        "pipecat-ai/pipecat#1": {"priority": "low", "why": "Big refactor"},
        "pipecat-ai/pipecat#2": {"priority": "normal"},
        "pipecat-ai/pipecat#3": {"depth": "quick", "priority": "high", "focus": ["typos"]},
    }
    await s.client.send_bus_message(Synced(source="github"))
    await eventually(lambda: len(s.client.of(ReviewStarted)) == 3)
    assert len(s.lead.notes) == 1  # one call for all three
    plans = {e.pr: e.plan for e in s.client.of(ReviewStarted)}
    assert plans["pipecat-ai/pipecat#3"]["depth"] == "quick"
    assert plans["pipecat-ai/pipecat#1"]["why"] == "Big refactor"
    assert [p.text for p in s.client.of(ReviewProgress)][:3] == ["Planning the review"] * 3

    s.agent.gate.set()
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 3)
    # Started highest priority first, and ran in that order.
    assert [run["pr"].number for run in s.agent.runs] == [3, 2, 1]
    quick = s.agent.runs[0]["plan"]
    assert quick.depth == "quick" and quick.focus == ("typos",) and quick.by == "lead"
    assert (await s.store.latest_review(PRRef(REF.repo, 1))).plan.why == "Big refactor"


async def test_reviews_go_ahead_when_the_lead_cant_plan(make):
    s = await make()
    s.lead.fail_with = "offline"
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.send_bus_message(Synced(source="github"))
    await eventually(lambda: s.client.of(ReviewFinished))
    plan = s.agent.runs[0]["plan"]
    assert (plan.depth, plan.by) == ("full", "default")
    assert "offline" in plan.why


async def test_quick_reviews_skip_the_repo_review_skill(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", CODE_REVIEW)
    s.lead.plans = {str(REF): {"depth": "quick"}}
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.send_bus_message(Synced(source="github"))
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] is None
    assert s.agent.runs[0]["prompt"].startswith("Review pipecat-ai/pipecat#2841")


async def test_reviews_you_ask_for_are_planned_at_high_priority(make):
    s = await make(auto_review=False)
    await s.store.upsert_pr(make_pr())
    s.lead.plans = {str(REF): {"priority": "low"}}

    response = await s.client.request(
        LEAD_REVIEW, {"prs": [str(REF), "pipecat-ai/pipecat#404"]}, worker=LEAD_WORKER
    )
    assert response["started"] == [str(REF)]
    assert response["errors"] == {
        "pipecat-ai/pipecat#404": "Purr doesn't know pipecat-ai/pipecat#404"
    }
    assert "the user asked for this review now" in s.lead.notes[0]
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["plan"].priority == "high"

    # Already being reviewed: says so.
    s.agent.gate = asyncio.Event()
    await s.client.request(LEAD_REVIEW, {"prs": [str(REF)]}, worker=LEAD_WORKER)
    response = await s.client.request(LEAD_REVIEW, {"prs": [str(REF)]}, worker=LEAD_WORKER)
    assert "already being reviewed" in response["errors"][str(REF)]
    s.agent.gate.set()


async def test_quick_reviews_use_the_quick_model_but_your_questions_dont(make):
    s = await make()
    s.lead.plans = {str(REF): {"depth": "quick"}}
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.send_bus_message(Synced(source="github"))
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["model"] == "quick-model"

    await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "Is the race real?"})
    await eventually(lambda: len(s.agent.runs) == 2)
    assert s.agent.runs[1]["model"] is None  # the agent's own, full model


async def test_new_commits_are_planned_too(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", CODE_REVIEW)
    await s.store.upsert_pr(make_pr(assigned_to_me=True))
    await s.client.send_bus_message(Synced(source="github"))
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.runs[0]["skill"] == "pipecat:code-review"  # a full review

    # A small follow-up push: the lead plans a quick re-review.
    s.repos.commits = (["Address review: guard the shutdown race"], ["M src/bus.py"])
    s.lead.plans = {str(REF): {"depth": "quick", "why": "Small fix for the race."}}
    await s.store.upsert_pr(make_pr(assigned_to_me=True, head_sha="def456"))
    await s.client.send_bus_message(
        PRChanged(source="github", pr=str(REF), change="new_commits", title="Add message bus")
    )
    await eventually(lambda: len(s.client.of(ReviewFinished)) == 2)
    notes = s.lead.notes[-1]
    assert (
        "Re-review: the author pushed new commits after the last review (which looked at abc123)"
        in notes
    )
    assert "  - Address review: guard the shutdown race" in notes and "  M src/bus.py" in notes
    run = s.agent.runs[1]
    assert run["resume"] is True and "git diff abc123 def456" in run["prompt"]
    assert run["plan"].why == "Small fix for the race."
    assert run["skill"] is None and run["model"] == "quick-model"  # quick: no skill, cheaper
    assert (await s.store.latest_review(REF)).plan.why == "Small fix for the race."


# -- Claude Code's questions ----------------------------------------------------------

PYTEST = (
    "Bash",
    {"command": "uv run pytest -x"},
    "Claude Code asks first",
    ["Bash(uv run pytest:*)"],
)


async def test_claude_codes_question_and_always_allow(make):
    s = await make(permission_wait=5)
    s.agent.ask = PYTEST
    await s.store.upsert_pr(make_pr())

    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(PermissionRequest))
    [asked] = s.client.of(PermissionRequest)
    assert (asked.rule, asked.reason) == ("Bash(uv run pytest:*)", "Claude Code asks first")

    answer = {"request_id": asked.request_id, "allow": True, "remember": "repo"}
    response = await s.client.request(PERMISSION_ANSWER, answer)
    assert response == {"ok": True, "rule": "Bash(uv run pytest:*)"}
    await eventually(lambda: s.client.of(ReviewFinished))
    # The agent gets your answer, and keeps Claude Code's rule for the repo.
    assert (s.agent.answers, s.agent.remembered) == ([True], ["repo"])


async def test_nothing_to_remember_without_a_suggested_rule(make):
    s = await make(permission_wait=5)
    s.agent.ask = ("WebFetch", {"url": "https://x"}, "")
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(PermissionRequest))
    [asked] = s.client.of(PermissionRequest)
    assert asked.rule == ""
    answer = {"request_id": asked.request_id, "allow": True, "remember": "all"}
    assert await s.client.request(PERMISSION_ANSWER, answer) == {"ok": True, "rule": None}
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.remembered == [None]


# -- What / commands use ------------------------------------------------------------


async def test_a_review_of_the_depth_you_ask_for_and_your_model(make, tmp_path):
    s = await make()
    s.repos.repo_plugin = make_plugin(tmp_path / "plugin", CODE_REVIEW)
    await s.store.upsert_pr(make_pr())
    s.lead.plans = {str(REF): {"depth": "full", "why": "Big change."}}

    await s.client.request(LEAD_REVIEW, {"prs": [str(REF)], "depth": "quick"}, worker=LEAD_WORKER)
    await eventually(lambda: s.client.of(ReviewFinished))
    plan = s.agent.runs[0]["plan"]
    assert (plan.depth, plan.why) == ("quick", "You asked for a quick review.")
    assert s.agent.runs[0]["model"] == "quick-model"

    # /model: everything in the PR's conversation uses yours, quick or not.
    assert await s.client.request(REVIEW_MODEL, {"pr": str(REF), "model": "opus"}) == {"ok": True}
    assert (await s.store.get_pr(REF)).model == "opus"
    await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "And the race?"})
    await eventually(lambda: len(s.agent.runs) == 2)
    assert s.agent.runs[1]["model"] == "opus"
    # A poll doesn't count it as GitHub changing the PR, nor forget it.
    from purr.sync import _same_metadata

    stored = await s.store.get_pr(REF)
    assert _same_metadata(stored, make_pr())
    await s.client.request(REVIEW_MODEL, {"pr": str(REF), "model": None})
    assert (await s.store.get_pr(REF)).model is None

    bad = await s.client.request(
        LEAD_REVIEW, {"prs": [str(REF)], "depth": "deep"}, worker=LEAD_WORKER
    )
    assert bad == {"error": "No such depth: deep"}

    skills = await s.client.request(REPO_SKILLS, {"repo": REF.repo})
    review_skill = {
        "name": "pipecat:code-review",
        "description": "Automated code review for pull requests",
    }
    assert skills == {"skills": [review_skill]}


async def test_what_background_agents_are_up_to_shows_but_isnt_kept(make):
    from purr.review.agent import describe_background

    s = await make()
    await s.store.upsert_pr(make_pr())
    s.agent.first = [
        {"kind": "task", "event": "started", "text": "Bug scan", "id": "a1"},
        {"kind": "background", "running": 1},
        {"kind": "task_progress", "id": "a1", "text": "Bug scan", "tool": "Grep", "tools": 14},
    ]
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))
    progress = [p.text for p in s.client.of(ReviewProgress)]
    assert "In the background: Bug scan" in progress
    assert "In the background: Bug scan · Grep · 14 tool uses" in progress
    # Status, not the conversation: neither is kept with the run's messages.
    kept = {m["kind"] for m in await s.store.messages(REF)}
    assert "task" in kept and not kept & {"task_progress", "background"}
    assert describe_background({"text": "Lint", "tools": 1}) == (
        "In the background: Lint · 1 tool use"
    )
