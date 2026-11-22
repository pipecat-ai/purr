"""The lead worker: decides what gets reviewed, how deep, in what order, and starts it."""

import asyncio
from dataclasses import replace

from loguru import logger
from pipecat.bus import BusJobRequestMessage, BusMessage
from pipecat.pipeline.job_context import JobParams
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from purr.git import GitRepos
from purr.github import GitHub
from purr.lead import LeadAgent, LeadInput, RepoContext, default_plan, lead_notes, plans_from
from purr.messages import (
    LEAD_REVIEW,
    LEAD_WORKER,
    REVIEW_REREVIEW,
    REVIEW_START,
    REVIEW_WORKER,
    PRChanged,
    ReviewProgress,
    Synced,
)
from purr.models import (
    PR,
    Depth,
    GHState,
    PRChange,
    PRDetails,
    Priority,
    PRRef,
    PRState,
    ReviewPlan,
    ReviewStatus,
)
from purr.review.instructions import ReviewInstructions
from purr.review.skills import repo_skills
from purr.store import Store


class LeadWorker(BaseWorker):
    """Plans reviews with the lead agent (:mod:`purr.lead`) and starts them.

    - After every poll, the open PRs assigned to you that were never reviewed
      (on startup, once the first poll has brought the store up to date), and
      PRs as they get assigned to you, are planned together and started.
    - New commits on a reviewed PR assigned to you get it re-reviewed, planned
      from what the new commits say and touch (with the new reviews of the same
      poll, in one call).
    - ``lead.review {prs}``: PRs you asked to review, planned at high priority.
      Answers once they've started: ``{"started": [...], "errors": {pr: error}}``.

    The first two only when ``auto_review``. Planning is one call per batch;
    if it fails, the PRs get a full review anyway, so nothing waits on it.

    Args:
        name: Bus name.
        store: PRs and reviews.
        agent: The lead agent.
        github: For PR details (description, files, CI); none: plan without them.
        repos: For the repos' skills; none: plan without them.
        instructions: Your review and code style; none: plan without them.
        auto_review: Review PRs assigned to you without being asked.
        settle: Seconds to wait for more changes before planning, so a poll's
            worth of them is planned together.
        batch: PRs per planning call.
    """

    def __init__(
        self,
        name: str = LEAD_WORKER,
        *,
        store: Store,
        agent: LeadAgent,
        github: GitHub | None = None,
        repos: GitRepos | None = None,
        instructions: ReviewInstructions | None = None,
        auto_review: bool = True,
        settle: float = 0.5,
        batch: int = 15,
    ):
        super().__init__(name)
        self._store = store
        self._agent = agent
        self._github = github
        self._repos = repos
        self._instructions = instructions
        self._auto_review = auto_review
        self._settle = settle
        self._batch = batch
        self._pending: dict[PRRef, str] = {}  # to plan on the next pass: review or rereview
        self._planning: set[PRRef] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def on_activated(self, args: dict | None) -> None:
        await super().on_activated(args)
        if self._task is None:
            self._task = self.create_task(self._loop(), f"{self}::plan")

    async def stop(self) -> None:
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        await super().stop()

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if not self._auto_review:
            return
        if isinstance(message, Synced):
            for pr in await self._store.list_prs(state=PRState.NOT_REVIEWED, open_only=True):
                if pr.assigned_to_me:
                    self._pending.setdefault(pr.ref, "review")
            self._wake.set()
        elif isinstance(message, PRChanged) and message.change == PRChange.ASSIGNED:
            self._pending.setdefault(PRRef.parse(message.pr), "review")
            self._wake.set()
        elif isinstance(message, PRChanged) and message.change == PRChange.NEW_COMMITS:
            self._pending[PRRef.parse(message.pr)] = "rereview"
            self._wake.set()

    @job(name=LEAD_REVIEW)
    async def on_lead_review(self, message: BusJobRequestMessage) -> None:
        try:
            refs = [PRRef.parse(str(pr)) for pr in (message.payload or {}).get("prs") or []]
        except ValueError as e:
            await self.send_job_response(message.job_id, {"error": str(e)})
            return
        depth = (message.payload or {}).get("depth")
        if depth not in (None, *Depth):
            await self.send_job_response(message.job_id, {"error": f"No such depth: {depth}"})
            return
        started, errors = await self.review(
            refs, requested=True, depth=Depth(depth) if depth else None
        )
        response = {"started": [str(ref) for ref in started], "errors": errors}
        await self.send_job_response(message.job_id, response)

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            await asyncio.sleep(self._settle)
            self._wake.clear()
            pending, self._pending = self._pending, {}
            reviews = sorted((ref for ref, kind in pending.items() if kind == "review"), key=str)
            again = sorted((ref for ref, kind in pending.items() if kind == "rereview"), key=str)
            try:
                await self.review(reviews, requested=False, rereviews=again)
            except Exception:
                logger.exception("Couldn't plan and start reviews")

    async def review(
        self,
        refs: list[PRRef],
        *,
        requested: bool,
        rereviews: list[PRRef] = (),
        depth: Depth | None = None,
    ) -> tuple[list[PRRef], dict[str, str]]:
        """Plan the PRs' reviews (and re-reviews) and start them, highest priority first.

        Unless ``requested``, only open PRs assigned to you that were never
        reviewed. ``rereviews`` are reviewed PRs with new commits; only those
        whose review can look at them (finished, assigned to you, head moved).
        ``depth`` overrides the lead's (you asked for a quick or full review).
        Returns what started, and why the rest didn't.
        """
        prs: list[PR] = []
        errors: dict[str, str] = {}
        for ref in dict.fromkeys(refs):
            pr = await self._store.get_pr(ref)
            if ref in self._planning:
                errors[str(ref)] = f"{ref} is already being planned"
            elif pr is None:
                errors[str(ref)] = f"Purr doesn't know {ref}"
            elif requested or (
                pr.state == PRState.NOT_REVIEWED
                and pr.assigned_to_me
                and pr.gh_state == GHState.OPEN
            ):
                prs.append(pr)
        again: list[PR] = []
        for ref in dict.fromkeys(rereviews):
            pr = await self._store.get_pr(ref)
            if pr and ref not in self._planning and await self._rereviewable(pr):
                again.append(pr)
        everything = prs + again
        if not everything:
            return [], errors
        self._planning.update(pr.ref for pr in everything)
        started: list[PRRef] = []
        rereviewing = {pr.ref for pr in again}
        try:
            for pr in everything:
                what = "re-review" if pr.ref in rereviewing else "review"
                await self._progress(pr.ref, f"Planning the {what}")
            plans = await self.plan(everything, requested=requested, rereviews=rereviewing)
            if depth:
                plans = {
                    ref: plan
                    if plan.depth == depth
                    else replace(plan, depth=depth, why=f"You asked for a {depth} review.")
                    for ref, plan in plans.items()
                }
            for pr in sorted(everything, key=lambda pr: plans[pr.ref].priority.rank):
                if pr.ref in rereviewing:
                    error = await self._rereview(pr.ref, plans[pr.ref])
                else:
                    error = await self._start(pr.ref, plans[pr.ref])
                if error:
                    errors[str(pr.ref)] = error
                    await self._progress(pr.ref, "")  # nothing's happening after all
                else:
                    started.append(pr.ref)
        finally:
            self._planning.difference_update(pr.ref for pr in everything)
        return started, errors

    async def _rereviewable(self, pr: PR) -> bool:
        """Whether a re-review would look at anything: the review the review worker continues."""
        review = await self._store.latest_review(pr.ref)
        return bool(
            pr.assigned_to_me
            and review
            and review.status == ReviewStatus.FINISHED
            and review.session_id
            and review.head_sha != pr.head_sha
        )

    async def plan(
        self, prs: list[PR], *, requested: bool = False, rereviews: set[PRRef] = frozenset()
    ) -> dict[PRRef, ReviewPlan]:
        """The lead's plan for each PR; Purr's default where it has none."""
        plans: dict[PRRef, ReviewPlan] = {}
        for start in range(0, len(prs), self._batch):
            chunk = prs[start : start + self._batch]
            reason = "the lead left it out"
            try:
                inputs, repos = await self._inputs(chunk, requested=requested, rereviews=rereviews)
                output = await self._agent.plan(lead_notes(inputs, repos))
                planned = plans_from(output, [pr.ref for pr in chunk])
            except Exception as e:
                logger.warning(f"The lead couldn't plan {len(chunk)} reviews: {e}")
                planned, reason = {}, f"the lead couldn't plan it ({e})"
            for pr in chunk:
                plan = planned.get(pr.ref) or default_plan(reason, requested=requested)
                if requested:  # you're waiting for it
                    plan = replace(plan, priority=Priority.HIGH)
                plans[pr.ref] = plan
        return plans

    async def _inputs(
        self, prs: list[PR], *, requested: bool, rereviews: set[PRRef]
    ) -> tuple[list[LeadInput], dict[str, RepoContext]]:
        details: dict[PRRef, PRDetails] = {}
        if self._github:
            try:
                details = await self._github.pr_details([pr.ref for pr in prs])
            except Exception as e:
                logger.warning(f"Planning without PR details: {e}")
        outcomes = {}
        for repo in {pr.repo for pr in prs}:
            outcomes.update(await self._store.outcomes(repo))
        inputs = []
        for pr in prs:
            item = LeadInput(
                pr=pr,
                details=details.get(pr.ref),
                previous=await self._store.latest_review(pr.ref),
                outcome=outcomes.get(pr.ref),
                requested=requested,
                rereview=pr.ref in rereviews,
            )
            since = item.previous.head_sha if item.previous else None
            if item.rereview and self._repos and since:
                commits = await self._repos.new_commits(pr, since)
                if commits:
                    item.new_commits, item.new_files = commits
            inputs.append(item)
        repos = {repo: await self._repo_context(repo) for repo in {pr.repo for pr in prs}}
        return inputs, repos

    async def _repo_context(self, repo: str) -> RepoContext:
        context = RepoContext(
            review_skill=await self._store.review_skill(repo) or None,
            instructions=self._instructions.for_repo(repo) if self._instructions else "",
        )
        if self._repos:
            try:
                context.skills = repo_skills(await self._repos.plugin(repo))
            except Exception as e:
                logger.warning(f"Planning without {repo}'s skills: {e}")
        return context

    async def _start(self, ref: PRRef, plan: ReviewPlan) -> str | None:
        """Start the review; the error if it couldn't."""
        payload = {"pr": str(ref), "plan": plan.to_dict()}
        params = JobParams(name=REVIEW_START, payload=payload, timeout=30)
        try:
            async with self.job(REVIEW_WORKER, params=params) as j:
                pass
        except Exception as e:
            return str(e)
        return (j.response or {}).get("error")

    async def _rereview(self, ref: PRRef, plan: ReviewPlan) -> str | None:
        """Have the PR's review look at its new commits; the error if it couldn't."""
        payload = {"pr": str(ref), "plan": plan.to_dict()}
        params = JobParams(name=REVIEW_REREVIEW, payload=payload, timeout=30)
        try:
            async with self.job(REVIEW_WORKER, params=params) as j:
                pass
        except Exception as e:
            return str(e)
        response = j.response or {}
        if not response.get("started"):
            return response.get("error") or f"{ref} wasn't re-reviewed"
        return None

    async def _progress(self, ref: PRRef, text: str) -> None:
        await self.send_bus_message(ReviewProgress(source=self.name, pr=str(ref), text=text))
