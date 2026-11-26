"""The lead worker: decides what gets reviewed, how deep, in what order, and starts it."""

import asyncio
from dataclasses import replace

from loguru import logger
from pipecat.bus import BusMessage
from pipecat.workers.base_worker import BaseWorker

from purr.compat import batched
from purr.git import GitRepos
from purr.github import GitHub
from purr.jobs import PurrError, handles, request
from purr.lead import LeadAgent, LeadInput, RepoContext, default_plan, lead_notes, plans_from
from purr.messages import (
    LEAD_WORKER,
    Planned,
    PlanReviews,
    PRChanged,
    PRListUpdated,
    Rereview,
    ReviewProgress,
    StartReview,
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
)
from purr.review.instructions import ReviewInstructions
from purr.review.skills import repo_skills
from purr.store import Store
from purr.workers.reviews import rereviewable


def _due_review(pr: PR) -> bool:
    """Whether Purr reviews the PR without being asked: open, assigned to you, never
    reviewed."""
    return pr.state == PRState.NOT_REVIEWED and pr.assigned_to_me and pr.gh_state == GHState.OPEN


def _at_depth(plans: dict[PRRef, ReviewPlan], depth: Depth) -> dict[PRRef, ReviewPlan]:
    """The plans, at the depth you asked for."""
    asked = f"You asked for a {depth} review."
    return {
        ref: plan if plan.depth == depth else replace(plan, depth=depth, why=asked)
        for ref, plan in plans.items()
    }


class LeadWorker(BaseWorker):
    """Plans reviews with the lead agent (:mod:`purr.lead`) and starts them.

    - After every poll, the open PRs assigned to you that were never reviewed
      (on startup, once the first poll has brought the store up to date), and
      PRs as they get assigned to you, are planned together and started.
    - New commits on a reviewed PR assigned to you get it re-reviewed, planned
      from what the new commits say and touch (with the new reviews of the same
      poll, in one call).
    - ``PlanReviews {prs, depth}``: PRs you asked to review, planned at high
      priority. Answers once they've started, with why any didn't (``Planned``).

    The first two only when ``auto_review``.
    Planning is one call per batch; if it fails, the PRs get a full review anyway,
    so nothing waits on it.

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
            unreviewed = await self._store.list_prs(state=PRState.NOT_REVIEWED, open_only=True)
            for ref in (pr.ref for pr in unreviewed if _due_review(pr)):
                self._pending.setdefault(ref, "review")
            self._wake.set()
        elif isinstance(message, PRChanged) and message.change == PRChange.ASSIGNED:
            self._pending.setdefault(PRRef.parse(message.pr), "review")
            self._wake.set()
        elif isinstance(message, PRChanged) and message.change == PRChange.NEW_COMMITS:
            self._pending[PRRef.parse(message.pr)] = "rereview"
            self._wake.set()

    @handles(PlanReviews)
    async def plan_reviews(self, request: PlanReviews) -> Planned:
        started, errors = await self.review(request.prs, requested=True, depth=request.depth)
        return Planned(started=started, errors=errors)

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
        prs, errors = await self._to_review(refs, requested=requested)
        again = await self._to_rereview(rereviews)
        everything = prs + again
        if not everything:
            return [], errors
        self._planning.update(pr.ref for pr in everything)
        try:
            started, failed = await self._plan_and_start(
                everything, requested=requested, rereviews={pr.ref for pr in again}, depth=depth
            )
        finally:
            self._planning.difference_update(pr.ref for pr in everything)
        return started, errors | failed

    async def _to_review(
        self, refs: list[PRRef], *, requested: bool
    ) -> tuple[list[PR], dict[str, str]]:
        """The PRs to review (unless ``requested``, only those due one), and why not the rest."""
        prs: list[PR] = []
        errors: dict[str, str] = {}
        for ref in dict.fromkeys(refs):
            pr = await self._store.get_pr(ref)
            if ref in self._planning:
                errors[str(ref)] = f"{ref} is already being planned"
            elif pr is None:
                errors[str(ref)] = f"Purr doesn't know {ref}"
            elif requested or (_due_review(pr) and not await self._store.latest_review(ref)):
                prs.append(pr)  # never reviewed: none you stopped, either
        return prs, errors

    async def _to_rereview(self, refs: list[PRRef]) -> list[PR]:
        """The PRs whose review can look at their new commits, unless already being planned."""
        prs: list[PR] = []
        for ref in dict.fromkeys(refs):
            pr = await self._store.get_pr(ref)
            if ref not in self._planning and rereviewable(pr, await self._store.latest_review(ref)):
                prs.append(pr)
        return prs

    async def _plan_and_start(
        self, prs: list[PR], *, requested: bool, rereviews: set[PRRef], depth: Depth | None
    ) -> tuple[list[PRRef], dict[str, str]]:
        """Plan the PRs' reviews and start them, highest priority first.

        Returns what started, and why the rest didn't.
        """
        for pr in prs:
            what = "re-review" if pr.ref in rereviews else "review"
            await self._progress(pr.ref, f"Planning the {what}")
        fresh = await self._under_way(prs)
        plans = await self.plan(prs, requested=requested, rereviews=rereviews)
        if depth:
            plans = _at_depth(plans, depth)
        started: list[PRRef] = []
        errors: dict[str, str] = {}
        for pr in sorted(prs, key=lambda pr: plans[pr.ref].priority.rank):
            start = self._rereview if pr.ref in rereviews else self._start
            if error := await start(pr.ref, plans[pr.ref]):
                errors[str(pr.ref)] = error
                await self._progress(pr.ref, "")  # nothing's happening after all
                if pr.ref in fresh:
                    await self._not_under_way(pr.ref)
                continue
            started.append(pr.ref)
        return started, errors

    async def _under_way(self, prs: list[PR]) -> set[PRRef]:
        """The PRs never reviewed are being reviewed from the moment their review is planned:
        they show as reviewing straight away (not first as not reviewed, once you're assigned
        them). Returns those."""
        fresh = {pr.ref for pr in prs if pr.state == PRState.NOT_REVIEWED}
        for ref in fresh:
            await self._store.set_pr_state(ref, PRState.REVIEWING)
        if fresh:
            await self.send_bus_message(PRListUpdated(source=self.name))
        return fresh

    async def _not_under_way(self, ref: PRRef) -> None:
        """A review that didn't start after all: not reviewed, as before (needing you, if
        something came to wait on you meanwhile)."""
        waits = await self._store.waits_on_you(ref)
        await self._store.set_pr_state(ref, PRState.NEEDS_YOU if waits else PRState.NOT_REVIEWED)
        await self.send_bus_message(PRListUpdated(source=self.name))

    async def plan(
        self, prs: list[PR], *, requested: bool = False, rereviews: set[PRRef] = frozenset()
    ) -> dict[PRRef, ReviewPlan]:
        """The lead's plan for each PR; Purr's default where it has none."""
        plans: dict[PRRef, ReviewPlan] = {}
        for batch in batched(prs, self._batch):
            plans |= await self._plan_batch(list(batch), requested=requested, rereviews=rereviews)
        return plans

    async def _plan_batch(
        self, prs: list[PR], *, requested: bool, rereviews: set[PRRef]
    ) -> dict[PRRef, ReviewPlan]:
        """One planning call for the PRs; Purr's default plan where the lead has none."""
        reason = "the lead left it out"
        try:
            inputs, repos = await self._inputs(prs, requested=requested, rereviews=rereviews)
            output = await self._agent.plan(lead_notes(inputs, repos))
            planned = plans_from(output, [pr.ref for pr in prs])
        except Exception as e:
            logger.warning(f"The lead couldn't plan {len(prs)} reviews: {e}")
            planned, reason = {}, f"the lead couldn't plan it ({e})"
        plans = {pr.ref: planned.get(pr.ref) or default_plan(reason) for pr in prs}
        if requested:  # you're waiting for it
            plans = {ref: replace(plan, priority=Priority.HIGH) for ref, plan in plans.items()}
        return plans

    async def _inputs(
        self, prs: list[PR], *, requested: bool, rereviews: set[PRRef]
    ) -> tuple[list[LeadInput], dict[str, RepoContext]]:
        details = await self._details(prs)
        repos = {pr.repo for pr in prs}
        outcomes = {}
        for repo in repos:
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
            if item.rereview:
                await self._add_new_commits(item)
            inputs.append(item)
        return inputs, {repo: await self._repo_context(repo) for repo in repos}

    async def _details(self, prs: list[PR]) -> dict[PRRef, PRDetails]:
        """The PRs' descriptions, files and CI; none when GitHub can't say."""
        if not self._github:
            return {}
        try:
            return await self._github.pr_details([pr.ref for pr in prs])
        except Exception as e:
            logger.warning(f"Planning without PR details: {e}")
            return {}

    async def _add_new_commits(self, item: LeadInput) -> None:
        """What a re-review's new commits say and change, when the clone can tell."""
        since = item.previous.head_sha if item.previous else None
        if not self._repos or not since:
            return
        if commits := await self._repos.new_commits(item.pr, since):
            item.new_commits, item.new_files = commits

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
        try:
            await request(self, StartReview(pr=ref, plan=plan), timeout=30)
        except PurrError as e:
            return str(e)
        return None

    async def _rereview(self, ref: PRRef, plan: ReviewPlan) -> str | None:
        """Have the PR's review look at its new commits; the error if it couldn't."""
        try:
            reply = await request(self, Rereview(pr=ref, plan=plan), timeout=30)
        except PurrError as e:
            return str(e)
        return None if reply.started else f"{ref} wasn't re-reviewed"

    async def _progress(self, ref: PRRef, text: str) -> None:
        await self.send_bus_message(ReviewProgress(source=self.name, pr=str(ref), text=text))
