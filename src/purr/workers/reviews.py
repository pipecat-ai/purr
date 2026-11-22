"""The review worker: one Claude review session per PR, in its own worktree."""

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from pathlib import Path

from loguru import logger
from pipecat.workers.base_worker import BaseWorker

from purr.git import GitRepos
from purr.github import GitHub, GitHubError
from purr.jobs import Busy, Done, Failed, Invalid, NotFound, PurrError, handles
from purr.messages import (
    REVIEW_WORKER,
    ActionPending,
    ActionResolved,
    AnswerPermission,
    ChooseReviewSkill,
    ListRepoSkills,
    ListReviewSkills,
    MessageSent,
    PermissionAnswered,
    PermissionRequest,
    PermissionResolved,
    PRListUpdated,
    RepoSkills,
    Rereview,
    Rereviewing,
    ReviewFailed,
    ReviewFinished,
    ReviewOpened,
    ReviewProgress,
    ReviewSkills,
    ReviewStarted,
    SendMessage,
    SessionMessage,
    SetModel,
    SkillChosen,
    SkillQuestion,
    StartReview,
)
from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Depth,
    Finding,
    GHState,
    Priority,
    PRRef,
    PRState,
    Review,
    ReviewPlan,
    ReviewStatus,
)
from purr.review.agent import (
    RESUME_PROMPT,
    AgentResult,
    PermissionAnswer,
    ReviewAgent,
    ReviewCallbacks,
    describe_background,
    describe_tool_use,
    replies_note,
    rereview_prompt,
    review_prompt,
    set_aside_note,
    thought_headline,
)
from purr.review.skills import repo_skills, review_skills
from purr.store import Store
from purr.workers.slots import Slots


def _repo(repo: str) -> str:
    if not repo:
        raise Invalid("Which repo?")
    return repo


def _metrics(result: AgentResult) -> dict:
    """A run's numbers, as Store.finish_run takes them."""
    return {
        "model": result.model,
        "cost_usd": result.cost_usd,
        "turns": result.turns,
        "duration_ms": result.duration_ms,
        "api_duration_ms": result.api_duration_ms,
        "usage": result.usage,
    }


@dataclass
class _Run:
    """One run of the agent on a PR, as it was launched (see ReviewWorker._launch)."""

    pr: PR  # as it was before the run
    review: Review
    kind: str  # what it's recorded as: review, resume, rereview or message
    plan: ReviewPlan
    prompt: str
    resume: bool
    followup: bool  # after the review (a re-review, your message): if it fails, the review stands
    queued: bool  # waits for one of the max_reviews slots (your messages don't)
    skill: str | None  # the repo skill it follows
    id: int = 0  # the store's, once it's recorded
    plugin: Path | None = None  # the repo's skills and agents, if they loaded
    submitted: dict = field(default_factory=dict)  # the agent's summary and findings

    @property
    def ref(self) -> PRRef:
        return self.pr.ref


class ReviewWorker(BaseWorker):
    """Runs reviews and follow-up turns, and records what they produce.

    What gets reviewed, how deep and when is up to the lead worker
    (:mod:`purr.workers.lead`); this one runs what it's asked to:

    - ``review.start {pr, plan}`` starts a review following ``plan`` (a
      :class:`ReviewPlan`; a full review without one) and responds right away
      with ``{"review_id": ...}`` (or ``{"error": ...}``); progress comes as events.
    - ``review.rereview {pr}`` has a finished review of a PR assigned to you
      look at its new commits, in the same session.
    - ``review.message {pr, text}`` sends a follow-up into the PR's session.
    - At most ``max_reviews`` reviews (and re-reviews) run at once; the rest
      wait in line by the plan's priority, then in order, saying where they
      are. Your own messages in a PR's session don't wait.
    - On startup, reviews interrupted by the last shutdown are resumed.
    - When Claude Code wants to ask about something (its permissions decide,
      see :mod:`purr.review.policy`), a :class:`PermissionRequest` goes out,
      the PR needs you, and the agent waits for a ``session.answer
      {request_id, allow, remember}`` job. ``remember`` (``repo`` or ``all``)
      keeps Claude Code's suggested rule (see :mod:`purr.review.rules`).
    - A full review follows the repo's own review skill when it has one (see
      :mod:`purr.review.skills`): one clearly for reviewing PRs is chosen
      without asking (:class:`SkillChosen`); otherwise, if some skills may be,
      a :class:`SkillQuestion` asks you once and the review waits a while for
      a ``review.skill {repo, skill}`` answer. ``review.skills {repo}`` lists
      them, for changing your mind.

    Stopping cancels running sessions; their reviews stay "running" in the
    store, which is how they get resumed next time.

    Args:
        name: Bus name.
        store: Where reviews, findings and actions are recorded.
        repos: Clones and worktrees.
        agent: Runs the agent sessions.
        github: Reads a PR's review threads for the agent (none: no threads).
        quick_model: Model for quick reviews and re-reviews (none: the agent's
            own); your messages in the PR's session always use the agent's own.
        permission_wait: Seconds an agent waits for a permission answer (and a
            review for an answer about the repo's review skill) before carrying
            on without it.
    """

    def __init__(
        self,
        name: str = REVIEW_WORKER,
        *,
        store: Store,
        repos: GitRepos,
        agent: ReviewAgent,
        github: GitHub | None = None,
        permission_wait: float = 300,
        max_reviews: int = 3,
        quick_model: str | None = None,
    ):
        super().__init__(name)
        self._store = store
        self._repos = repos
        self._agent = agent
        self._github = github
        self._permission_wait = permission_wait
        self._quick_model = quick_model
        self._running: dict[PRRef, asyncio.Task] = {}
        self._start_lock = asyncio.Lock()
        self._startup_task: asyncio.Task | None = None
        self._asks: dict[str, asyncio.Future[PermissionAnswer]] = {}
        self._ask_prs: dict[str, PRRef] = {}  # request id -> the PR whose agent is asking
        # What you write to a PR's agent while it works, for it to take up as it goes.
        self._inboxes: dict[PRRef, asyncio.Queue[str]] = {}
        self._run_ids: dict[PRRef, int] = {}
        self._ask_rules: dict[str, str] = {}  # request id -> the rule "always" would keep
        self._slots = Slots(max_reviews)
        # Per repo (lowercase): its review skill question, and until when reviews wait for it.
        self._skill_asks: dict[str, tuple[asyncio.Future[str | None], float]] = {}
        self._skill_locks: dict[str, asyncio.Lock] = {}

    async def on_activated(self, args: dict | None) -> None:
        await super().on_activated(args)
        self._startup_task = self.create_task(self._startup(), f"{self}::startup")

    async def stop(self) -> None:
        tasks = [t for t in [self._startup_task, *self._running.values()] if t]
        for task in tasks:
            await self.cancel_task(task, timeout=5)
        self._running.clear()
        await super().stop()

    @property
    def running(self) -> set[PRRef]:
        return set(self._running)

    async def _startup(self) -> None:
        await self._store.interrupt_runs()  # cut short by the last shutdown
        # A follow-up cut short (Purr quit mid-turn) leaves its PR "reviewing".
        settled = False
        for pr in await self._store.list_prs(state=PRState.REVIEWING):
            review = await self._store.latest_review(pr.ref)
            if review is None or review.status != ReviewStatus.RUNNING:
                pending = await self._store.waits_on_you(pr.ref)
                await self._store.set_pr_state(
                    pr.ref, PRState.NEEDS_YOU if pending or review is None else PRState.DONE
                )
                settled = True
        if settled:
            await self.send_bus_message(PRListUpdated(source=self.name))
        for review in await self._store.interrupted_reviews():
            pr = await self._store.get_pr(review.pr)
            if pr is None or pr.gh_state != GHState.OPEN:
                await self._store.fail_review(review.id, error="The PR is no longer open.")
                continue
            if pr.ref not in self._running:
                logger.info(f"Resuming the review of {pr.ref}")
                self._launch(pr, review, prompt=RESUME_PROMPT, resume=True, kind="resume")

    async def rereview(self, ref: PRRef, *, plan: ReviewPlan | None = None) -> bool:
        """Have a finished review look at the PR's new commits, in the same session.

        ``plan`` is what the lead planned for it (default: the review's last
        plan); a quick one doesn't follow the repo's review skill. Returns
        whether a re-review started.
        """
        async with self._start_lock:
            pr = await self._store.get_pr(ref)
            review = await self._store.latest_review(ref)
            if (
                ref in self._running
                or pr is None
                or not pr.assigned_to_me
                or review is None
                or review.status != ReviewStatus.FINISHED
                or not review.session_id
                or review.head_sha == pr.head_sha
            ):
                return False
            if plan:
                await self._store.set_review_plan(review.id, plan)
            plan = plan or review.plan or ReviewPlan()
            skill = review.skill if plan.depth == Depth.FULL else None
            prompt = rereview_prompt(review.head_sha, pr.head_sha, skill)
            self._launch(
                pr,
                review,
                prompt=prompt,
                resume=True,
                followup=True,
                kind="rereview",
                plan=plan,
            )
        await self._progress(ref, "Re-reviewing new commits")
        return True

    # -- Jobs ---------------------------------------------------------------------

    @handles(StartReview)
    async def handle_start_review(self, request: StartReview) -> ReviewOpened:
        review = await self.start_review(request.pr, plan=request.plan)
        return ReviewOpened(review_id=review.id)

    @handles(Rereview)
    async def handle_rereview(self, request: Rereview) -> Rereviewing:
        return Rereviewing(started=await self.rereview(request.pr, plan=request.plan))

    @handles(SendMessage)
    async def send_message(self, request: SendMessage) -> MessageSent:
        sent = await self.send_followup(request.pr, request.text)
        await self._store.add_prompt(request.pr, request.text)  # for the prompt's history
        return sent

    @handles(AnswerPermission)
    async def answer_permission(self, request: AnswerPermission) -> PermissionAnswered:
        future = self._asks.get(request.request_id)
        if future is None or future.done():
            raise NotFound("Nothing is waiting for that")
        rule = self._ask_rules.get(request.request_id)
        remember = request.remember if request.allow and rule else None
        future.set_result(PermissionAnswer(allowed=request.allow, remember=remember))
        return PermissionAnswered(rule=rule if remember else None)

    @handles(SetModel)
    async def set_model(self, request: SetModel) -> Done:
        model = request.model.strip() if request.model else None
        await self._store.set_pr_model(request.pr, model or None)
        return Done()

    @handles(ListRepoSkills)
    async def list_repo_skills(self, request: ListRepoSkills) -> RepoSkills:
        return RepoSkills(skills=repo_skills(await self._repo_plugin(_repo(request.repo))))

    @handles(ListReviewSkills)
    async def list_review_skills(self, request: ListReviewSkills) -> ReviewSkills:
        repo = _repo(request.repo)
        skills = review_skills(await self._repo_plugin(repo))
        return ReviewSkills(skills=skills, chosen=await self._store.review_skill(repo))

    @handles(ChooseReviewSkill)
    async def choose_review_skill(self, request: ChooseReviewSkill) -> Done:
        repo, skill = _repo(request.repo), request.skill
        if skill is not None:
            await self._store.set_review_skill(repo, skill, chosen_by="you")
            await self.send_bus_message(
                SkillChosen(source=self.name, repo=repo, skill=skill, chosen_by="you")
            )
        # Reviews waiting on the question go ahead (None: without a skill, for now).
        asked = self._skill_asks.get(repo.lower())
        if asked and not asked[0].done():
            asked[0].set_result(skill)
        return Done()

    # -- Starting -------------------------------------------------------------------

    async def start_review(self, ref: PRRef, *, plan: ReviewPlan | None = None) -> Review:
        """Start reviewing a PR in the background, following ``plan`` (default: a full review)."""
        plan = plan or ReviewPlan()
        async with self._start_lock:
            if ref in self._running:
                raise Busy(f"{ref} is already being reviewed")
            pr = await self._store.get_pr(ref)
            if pr is None:
                raise NotFound(f"Purr doesn't know {ref}")
            # A new review supersedes whatever the last one proposed.
            await self._dismiss_pending(ref)
            review = await self._store.start_review(
                ref,
                worktree_path=str(self._repos.worktree_path(ref)),
                head_sha=pr.head_sha,
                plan=plan,
            )
            await self._store.set_review_session(review.id, str(uuid.uuid4()))
            review = await self._store.latest_review(ref)
            assert review is not None
            await self._store.set_pr_state(ref, PRState.REVIEWING)
            # The prompt depends on the repo's review skill, which may need asking.
            self._launch(pr, review, prompt="", resume=False, kind="review")
        await self.send_bus_message(
            ReviewStarted(source=self.name, pr=str(ref), plan=plan.to_dict())
        )
        await self.send_bus_message(PRListUpdated(source=self.name))
        return review

    async def send_followup(self, ref: PRRef, text: str) -> MessageSent:
        """Continue a PR's session with a message from the user.

        As in Claude Code: writing while the agent works goes to it straight
        away, for it to take up at its next step. Writing when it waits on you
        to decide something is deciding it: to a permission request, it's a no,
        and what to do instead; a proposal is set aside, and the agent proposes
        again if it still stands. A slash command (``/compact``) decides nothing,
        and waits until the agent's done.

        Returns what it did besides: answered a permission request, set proposals
        aside, or went to the agent at work.
        """
        if not text.strip():
            raise Invalid("Nothing to send")
        command = text.lstrip().startswith("/")  # /compact, a skill: not a reply
        async with self._start_lock:
            if ref in self._running:
                return await self._write_while_working(ref, text, command=command)
            return await self._write_new_turn(ref, text, command=command)

    async def _write_while_working(self, ref: PRRef, text: str, *, command: bool) -> MessageSent:
        """To the agent at work: a no to what it asks, with what to do instead, or a message
        it takes up at its next step. A command has to wait until it's done."""
        if command or ref not in self._inboxes:
            raise Busy(f"The agent is still working on {ref}")
        if self._answer_instead(ref, text):
            return MessageSent(answered=True)
        self._inboxes[ref].put_nowait(text)
        if (run_id := self._run_ids.get(ref)) is not None:
            await self._store.add_message(
                run_id, ref, {"kind": "prompt", "text": text}, author="you"
            )
        return MessageSent(while_working=True)

    async def _write_new_turn(self, ref: PRRef, text: str, *, command: bool) -> MessageSent:
        """A new turn in the PR's session, with what you wrote. Unless it's a command, it sets
        aside what the agent proposed, and tells it about replies to you on GitHub."""
        review = await self._store.latest_review(ref)
        pr = await self._store.get_pr(ref)
        if review is None or pr is None or not review.session_id:
            raise NotFound(f"{ref} hasn't been reviewed yet")
        sent = MessageSent()
        if not command:
            pr, sent.set_aside = await self._set_proposals_aside(pr)
            if sent.set_aside:
                text = set_aside_note(sent.set_aside) + text
            if pr.replies:  # so it can help you answer them
                text = replies_note(pr.replies) + text
        # You're waiting on this one: it doesn't queue behind reviews.
        self._launch(
            pr, review, prompt=text, resume=True, followup=True, queued=False, kind="message"
        )
        return sent

    async def _set_proposals_aside(self, pr: PR) -> tuple[PR, list[str]]:
        """Dismiss what the PR's agent proposed, since you wrote instead of deciding it.

        Returns the PR as it is now, and the proposals set aside.
        """
        proposals = await self._store.pending_actions(pr.ref)
        if not proposals:
            return pr, []
        await self._dismiss_pending(pr.ref)
        # Nothing left to decide, as with x (unless there are replies to answer).
        if pr.state == PRState.NEEDS_YOU and not await self._store.waits_on_you(pr.ref):
            await self._store.set_pr_state(pr.ref, PRState.DONE)
            pr = replace(pr, state=PRState.DONE)
        return pr, [proposal.description for proposal in proposals]

    async def _send_after(self, ref: PRRef, text: str) -> None:
        """What you wrote that the agent didn't get to read in its run, as a follow-up."""
        try:
            await self.send_followup(ref, text)
        except PurrError as e:
            logger.warning(f"Couldn't send what you wrote to {ref}'s agent: {e}")

    def _answer_instead(self, ref: PRRef, text: str) -> bool:
        """No to what the PR's agent asks to do, with what to do instead; whether it asked."""
        for request_id, asking in self._ask_prs.items():
            future = self._asks.get(request_id)
            if asking == ref and future is not None and not future.done():
                future.set_result(PermissionAnswer(allowed=False, instead=text))
                return True
        return False

    def _launch(
        self,
        pr: PR,
        review: Review,
        *,
        prompt: str,
        resume: bool,
        followup: bool = False,
        queued: bool = True,
        kind: str,
        plan: ReviewPlan | None = None,
    ):
        """Run the agent on ``pr`` in the background, following ``plan`` (default: the review's).

        ``kind`` (review, resume, rereview, message) is what the run is
        recorded as, with every message in it (see Store.start_run). A new
        review (``review``) makes its own prompt.
        """
        plan = plan or review.plan or ReviewPlan()
        # A quick re-review doesn't follow the repo's review skill.
        skill = None if kind == "rereview" and plan.depth == Depth.QUICK else review.skill
        run = _Run(
            pr=pr,
            review=review,
            kind=kind,
            plan=plan,
            prompt=prompt,
            resume=resume,
            followup=followup,
            queued=queued,
            skill=skill,
        )
        self._inboxes[pr.ref] = asyncio.Queue()
        self._running[pr.ref] = self.create_task(self._run(run), f"{self}::review::{pr.ref}")

    async def _take_slot(self, ref: PRRef, priority: Priority) -> None:
        """Wait for one of the ``max_reviews`` slots, in line by priority, then arrival."""
        turn = self._slots.join(ref, priority.rank)
        if turn.done():
            return
        try:
            await self._report_queue()
            await turn
        except asyncio.CancelledError:
            self._slots.leave(turn)
            raise
        await self._report_queue()

    async def _report_queue(self) -> None:
        for ahead, ref in enumerate(self._slots.waiting()):
            # Waiting for its turn: nothing's working on it yet.
            text = f"Queued, {ahead} ahead" if ahead else "Queued, next up"
            await self._progress(ref, text, waiting=True)

    # -- Running ----------------------------------------------------------------------

    async def _repo_plugin(self, repo: str) -> Path | None:
        """The repo's skills and agents; a review goes ahead without them if they fail."""
        try:
            return await self._repos.plugin(repo)
        except Exception as e:
            logger.warning(f"Couldn't load {repo}'s skills and agents: {e}")
            return None

    async def _choose_skill(self, ref: PRRef, plugin: Path | None) -> str | None:
        """The repo skill a new review follows; None for Purr's own review.

        The repo's choice is kept. Until there is one: a skill clearly for
        reviewing PRs is chosen (and announced); otherwise, when some skills
        may be, you're asked, and the review waits a while for your answer.
        """
        skills = review_skills(plugin)
        names = {skill.name for skill in skills}
        async with self._skill_locks.setdefault(ref.repo.lower(), asyncio.Lock()):
            chosen = await self._store.review_skill(ref.repo)
            if chosen is not None:
                return chosen if chosen in names else None  # gone from the repo: Purr's own
            likely = [skill.name for skill in skills if skill.likely]
            if len(likely) == 1:
                await self._store.set_review_skill(ref.repo, likely[0], chosen_by="purr")
                await self.send_bus_message(
                    SkillChosen(source=self.name, repo=ref.repo, skill=likely[0], chosen_by="purr")
                )
                return likely[0]
            if not skills:
                return None
            answer = await self._ask_skill(ref, [asdict(skill) for skill in skills])
            return answer if answer in names else None

    async def _ask_skill(self, ref: PRRef, skills: list[dict]) -> str | None:
        """Ask which skill the repo's reviews follow (once per run), and wait a while."""
        key = ref.repo.lower()
        if key not in self._skill_asks:
            future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
            self._skill_asks[key] = (future, time.monotonic() + self._permission_wait)
            await self.send_bus_message(
                SkillQuestion(source=self.name, repo=ref.repo, skills=skills)
            )
        future, until = self._skill_asks[key]
        if future.done():
            return future.result()
        await self._progress(
            ref, "Waiting for you: should reviews here follow a repo skill?", waiting=True
        )
        try:
            return await asyncio.wait_for(asyncio.shield(future), until - time.monotonic())
        except TimeoutError:
            return None

    async def _run(self, run: _Run) -> None:
        """Run the agent on the PR in its turn, and record what came of it."""
        ref, review = run.ref, run.review
        run.id = await self._store.start_run(
            ref, kind=run.kind, review_id=review.id, session_id=review.session_id
        )
        self._run_ids[ref] = run.id
        slot = stopping = False
        result: AgentResult | None = None
        try:
            await self._prepare(run)
            if run.queued:
                await self._take_slot(ref, run.plan.priority)
                slot = True
            result = await self._run_agent(run)
            if result.is_error:
                raise Failed(result.error or "The agent session failed")
            await self._finished(run, result)
        except asyncio.CancelledError:
            stopping = True
            raise  # Purr is stopping; the review stays "running" and resumes next time
        except Exception as e:
            logger.exception(f"Review of {ref} failed")
            await self._failed(run, e, result)
        finally:
            if slot:
                self._slots.release()
            self._end_run(ref, stopping=stopping)

    async def _prepare(self, run: _Run) -> None:
        """What a run needs before its turn: the repo's skills and, for a review, its prompt."""
        if run.followup:
            await self._store.set_pr_state(run.ref, PRState.REVIEWING)
            await self.send_bus_message(PRListUpdated(source=self.name))
        run.plugin = await self._repo_plugin(run.ref.repo)
        if run.kind != "review":
            return
        # Only full reviews follow the repo's review process.
        full = run.plan.depth == Depth.FULL
        run.skill = await self._choose_skill(run.ref, run.plugin) if full else None
        await self._store.set_review_followed(run.review.id, run.skill)
        run.prompt = review_prompt(run.pr, run.skill)

    async def _run_agent(self, run: _Run) -> AgentResult:
        """Check out the PR and run the agent in its worktree."""
        ref, session_id = run.ref, run.review.session_id
        await self._store.run_started(run.id)
        await self._progress(ref, "Checking out the PR")
        worktree = await self._repos.checkout(ref, run.pr.head_sha)
        assert session_id
        if run.resume and not self._agent.session_exists(session_id, worktree):
            # It never really started: begin again, under the same session ID.
            run.resume, run.prompt = False, review_prompt(run.pr, run.skill)
        await self._progress(ref, "Reviewing" if not run.followup else "Thinking")
        author = "you" if run.kind == "message" else "purr"
        await self._store.add_message(
            run.id, ref, {"kind": "prompt", "text": run.prompt}, author=author
        )
        return await self._agent.run(
            pr=run.pr,
            worktree=worktree,
            session_id=session_id,
            resume=run.resume,
            prompt=run.prompt,
            callbacks=self._callbacks(run),
            guidelines=await self._repos.guidelines(ref.repo),
            plugin=run.plugin,
            skill=run.skill,
            plan=run.plan,
            model=await self._model(ref, run.plan, run.kind),
            inbox=self._inboxes.get(ref),
        )

    async def _finished(self, run: _Run, result: AgentResult) -> None:
        """Record a run that went well. A review, or a follow-up that submitted one, is
        finished; then the PR needs you, or a follow-up's goes back to how it was."""
        await self._store.finish_run(run.id, result=_metrics(result))
        if run.submitted or not run.followup:
            summary = run.submitted.get("summary") or result.text or "No summary."
            findings: list[Finding] = run.submitted.get("findings", [])
            await self._store.finish_review(
                run.review.id, summary=summary, findings=findings, head_sha=run.pr.head_sha
            )
            await self.send_bus_message(
                ReviewFinished(
                    source=self.name,
                    pr=str(run.ref),
                    summary=summary,
                    findings=[asdict(f) for f in findings],
                )
            )
        await self._settle(run.ref, run.pr.state if run.followup else PRState.NEEDS_YOU)

    async def _failed(self, run: _Run, error: Exception, result: AgentResult | None) -> None:
        """Record a run that failed. A follow-up's failure is said in the PR's session, and
        the PR goes back to how it was; a review fails, and the PR needs you."""
        entry = {"kind": "error", "text": str(error)}
        await self._store.add_message(run.id, run.ref, entry)
        await self._store.finish_run(
            run.id, error=str(error), result=_metrics(result) if result else None
        )
        if run.followup:
            await self.send_bus_message(
                SessionMessage(source=self.name, pr=str(run.ref), message=entry)
            )
            await self._settle(run.ref, run.pr.state)
            return
        await self._store.fail_review(run.review.id, error=str(error))
        await self.send_bus_message(
            ReviewFailed(source=self.name, pr=str(run.ref), error=str(error))
        )
        await self._settle(run.ref, PRState.NEEDS_YOU)

    def _end_run(self, ref: PRRef, *, stopping: bool) -> None:
        """Forget the PR's run. What you wrote that its agent didn't read goes in a new
        turn, unless Purr is stopping."""
        self._running.pop(ref, None)
        self._run_ids.pop(ref, None)
        inbox = self._inboxes.pop(ref, None)
        unread = []
        while inbox is not None and not inbox.empty():
            unread.append(inbox.get_nowait())
        if unread and not stopping:
            # Written as it finished, or to a CLI that can't take it mid-run.
            self.create_task(self._send_after(ref, "\n\n".join(unread)))

    async def _model(self, ref: PRRef, plan: ReviewPlan, kind: str) -> str | None:
        """The model for a run: yours for the PR (/model), else the cheaper one for quick
        work; you asking gets the full one. None: the agent's own."""
        pr = await self._store.get_pr(ref)
        if pr and pr.model:
            return pr.model
        if plan.depth == Depth.QUICK and kind != "message":
            return self._quick_model
        return None

    async def _settle(self, ref: PRRef, state: PRState) -> None:
        """Set the PR's state after a run: anything waiting on you means it needs you."""
        await self._progress(ref, "")  # nothing's happening any more
        if await self._store.waits_on_you(ref):
            state = PRState.NEEDS_YOU
        await self._store.set_pr_state(ref, state)
        await self.send_bus_message(PRListUpdated(source=self.name))

    async def _progress(self, ref: PRRef, text: str, *, waiting: bool = False) -> None:
        await self.send_bus_message(
            ReviewProgress(source=self.name, pr=str(ref), text=text, waiting=waiting)
        )

    async def _dismiss_pending(self, ref: PRRef) -> None:
        for action in await self._store.pending_actions(ref):
            resolved = await self._store.transition_action(
                action.id, expected=ActionStatus.PENDING, new=ActionStatus.DISMISSED
            )
            if resolved:
                await self.send_bus_message(
                    ActionResolved(
                        source=self.name, pr=str(ref), action_id=action.id, approved=False
                    )
                )

    # -- What the agent reports while it runs -------------------------------------------

    def _callbacks(self, run: _Run) -> ReviewCallbacks:
        """How the agent reports back during ``run``."""
        return ReviewCallbacks(
            on_message=partial(self._on_message, run),
            on_submit=partial(self._on_submit, run),
            on_propose=partial(self._on_propose, run.ref),
            on_ask=partial(self._on_ask, run),
            on_threads=partial(self._on_threads, run.ref),
        )

    async def _on_message(self, run: _Run, entry: dict) -> None:
        """A transcript entry: recorded, shown in the PR's session, and said in its status."""
        if entry["kind"] == "task_progress":  # what a background task's up to: status
            await self._progress(run.ref, describe_background(entry))
            return
        if entry["kind"] != "background":  # how many are out: status too
            await self._store.add_message(run.id, run.ref, entry)
        await self.send_bus_message(
            SessionMessage(source=self.name, pr=str(run.ref), message=entry)
        )
        if entry["kind"] == "tool_use":
            await self._progress(run.ref, describe_tool_use(entry["name"], entry["input"]))
        elif entry["kind"] == "task" and entry.get("event") == "started":
            await self._progress(run.ref, describe_background(entry))
        elif entry["kind"] == "thinking":
            await self._progress(run.ref, f"Thinking: {thought_headline(entry['text'])}")

    async def _on_submit(self, run: _Run, summary: str, findings: list[Finding]) -> None:
        run.submitted["summary"] = summary
        run.submitted["findings"] = findings

    async def _on_propose(
        self, ref: PRRef, kind: ActionKind, description: str, payload: dict
    ) -> str:
        """Keep what the agent proposes posting, for you to approve; returns its action ID."""
        await self._dismiss_pending(ref)  # a new recommendation replaces the old one
        action = await self._store.add_pending_action(
            ref, kind=kind, description=description, payload=payload
        )
        await self.send_bus_message(
            ActionPending(
                source=self.name, pr=str(ref), action_id=action.id, description=description
            )
        )
        return action.id

    async def _on_ask(
        self, run: _Run, tool: str, tool_input: dict, reason: str, rules: list[str] = ()
    ) -> PermissionAnswer:
        """Ask you about a tool call the agent wants to make, and wait a while for the answer."""
        # The agent waits for the user, for a while; meanwhile the PR needs them.
        # Claude Code itself gives up after about ten minutes, and after that
        # the session refuses everything, so Purr answers first.
        description = describe_tool_use(tool, tool_input)
        request_id, future = self._open_ask(run.ref, rules)
        await self._store.set_pr_state(run.ref, PRState.NEEDS_YOU)
        asked = {"tool": tool, "input": tool_input, "description": description, "reason": reason}
        await self._store.add_message(
            run.id, run.ref, {"kind": "permission_request", **asked}, author="agent"
        )
        await self.send_bus_message(
            PermissionRequest(
                source=self.name,
                pr=str(run.ref),
                request_id=request_id,
                tool=tool,
                description=description,
                reason=reason,
                rule=", ".join(rules),
            )
        )
        await self.send_bus_message(PRListUpdated(source=self.name))
        answer = await self._wait_for_answer(request_id, future)
        await self._answered(run, request_id, description, answer)
        return answer

    def _open_ask(
        self, ref: PRRef, rules: list[str]
    ) -> tuple[str, asyncio.Future[PermissionAnswer]]:
        """A new permission request from the PR's agent, for you (or your writing) to answer."""
        request_id = uuid.uuid4().hex
        future: asyncio.Future[PermissionAnswer] = asyncio.get_running_loop().create_future()
        self._asks[request_id] = future
        self._ask_prs[request_id] = ref
        if rules:
            self._ask_rules[request_id] = ", ".join(rules)
        return request_id, future

    async def _wait_for_answer(
        self, request_id: str, future: asyncio.Future[PermissionAnswer]
    ) -> PermissionAnswer:
        """Your answer to a permission request; no answer if it doesn't come in time."""
        try:
            return await asyncio.wait_for(asyncio.shield(future), self._permission_wait)
        except TimeoutError:
            return PermissionAnswer(allowed=None)
        finally:
            self._asks.pop(request_id, None)
            self._ask_prs.pop(request_id, None)
            self._ask_rules.pop(request_id, None)

    async def _answered(
        self, run: _Run, request_id: str, description: str, answer: PermissionAnswer
    ) -> None:
        """Back to work after a permission request: record and say how it was answered."""
        answered = answer.allowed is not None
        await self._store.set_pr_state(run.ref, PRState.REVIEWING)
        entry = {
            "kind": "permission_answer",
            "allowed": bool(answer.allowed),
            "answered": answered,
            "remember": answer.remember,
            **({"instead": answer.instead} if answer.instead else {}),
        }
        await self._store.add_message(run.id, run.ref, entry, author="you" if answered else "purr")
        await self.send_bus_message(
            PermissionResolved(
                source=self.name,
                pr=str(run.ref),
                request_id=request_id,
                allowed=bool(answer.allowed),
                answered=answered,
                description=description,
            )
        )
        await self.send_bus_message(PRListUpdated(source=self.name))

    async def _on_threads(self, ref: PRRef) -> list[dict]:
        """The PR's review threads, for the agent; none when GitHub can't say."""
        if not self._github:
            return []
        try:
            return await self._github.review_threads(ref)
        except GitHubError as e:
            logger.warning(f"Couldn't read the review threads of {ref}: {e}")
            return []
