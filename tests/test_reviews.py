"""The review worker on a real bus, with a scripted agent."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pipecat.bus import BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from purr.messages import (
    PERMISSION_ANSWER,
    REVIEW_MESSAGE,
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
from purr.workers import ActivityWorker, ReviewWorker
from tests.fakes import FakeAgent, FakeRepos, make_plugin

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

    async def request(self, name: str, payload: dict) -> dict:
        params = JobParams(name=name, payload=payload, timeout=5)
        async with self.job(REVIEW_WORKER, params=params) as j:
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
    def __init__(self, store, agent, repos, client, runner):
        self.store, self.agent, self.repos, self.client, self.runner = (
            store,
            agent,
            repos,
            client,
            runner,
        )

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
        agent, repos, client = FakeAgent(), FakeRepos(tmp_path), Client()
        if before_start:
            await before_start(store, agent)
        worker = ReviewWorker(
            store=store,
            repos=repos,
            agent=agent,
            auto_review=auto_review,
            permission_wait=permission_wait,
            max_reviews=max_reviews,
        )
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(ActivityWorker(store=store), worker, client)
        task = asyncio.create_task(runner.run(auto_end=False))

        async def cleanup():
            await runner.end()
            await asyncio.wait_for(task, 5)
            await store.close()

        cleanups.append(cleanup)
        await asyncio.sleep(0.05)
        return Setup(store, agent, repos, client, runner)

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
    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "why?"})
    assert "still working" in response["error"]
    s.agent.gate.set()
    await eventually(lambda: s.client.of(ReviewFinished))


async def test_follow_up_continues_the_session(make):
    s = await make()
    await s.store.upsert_pr(make_pr())
    await s.client.request(REVIEW_START, {"pr": str(REF)})
    await eventually(lambda: s.client.of(ReviewFinished))

    response = await s.client.request(REVIEW_MESSAGE, {"pr": str(REF), "text": "Is the race real?"})
    assert response == {"ok": True}
    await eventually(lambda: len(s.agent.runs) == 2)
    followup = s.agent.runs[1]
    assert followup["resume"] is True and followup["prompt"] == "Is the race real?"
    assert followup["session_id"] == s.agent.runs[0]["session_id"]


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
    assert await s.client.request(PERMISSION_ANSWER, answer) == {"ok": True}
    await eventually(lambda: s.client.of(ReviewFinished))
    assert s.agent.answers == [True]
    assert "error" in await s.client.request(PERMISSION_ANSWER, answer)  # already answered


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
    await eventually(lambda: s.agent.runs[-1]["prompt"] == "and the tests?")
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
    await eventually(lambda: s.agent.runs[-1]["prompt"] == "is it safe?")

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
    assert messages[2]["data"] == {"kind": "permission_answer", "allowed": True, "answered": True}
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
