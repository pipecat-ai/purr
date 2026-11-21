"""The review worker: one Claude review session per PR, in its own worktree."""

import asyncio
import uuid
from dataclasses import asdict

from loguru import logger
from pipecat.bus import BusJobRequestMessage, BusMessage
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from purr.git import GitRepos
from purr.github import GitHub, GitHubError
from purr.messages import (
    PERMISSION_ANSWER,
    REVIEW_MESSAGE,
    REVIEW_START,
    REVIEW_WORKER,
    ActionPending,
    ActionResolved,
    PermissionRequest,
    PermissionResolved,
    PRChanged,
    PRListUpdated,
    ReviewFailed,
    ReviewFinished,
    ReviewProgress,
    ReviewStarted,
    SessionMessage,
)
from purr.models import (
    PR,
    ActionKind,
    ActionStatus,
    Finding,
    GHState,
    PRChange,
    PRRef,
    PRState,
    Review,
    ReviewStatus,
)
from purr.review.agent import (
    RESUME_PROMPT,
    ReviewAgent,
    ReviewCallbacks,
    describe_tool_use,
    rereview_prompt,
    review_prompt,
)
from purr.store import Store


class ReviewError(Exception):
    """A review couldn't be started or didn't complete."""


class ReviewWorker(BaseWorker):
    """Runs reviews and follow-up turns, and records what they produce.

    - ``review.start {pr}`` starts a review and responds right away with
      ``{"review_id": ...}`` (or ``{"error": ...}``); progress comes as events.
    - ``review.message {pr, text}`` sends a follow-up into the PR's session.
    - PRs assigned to you are reviewed automatically (when ``auto_review``).
    - On startup, reviews interrupted by the last shutdown are resumed.
    - When a reviewed PR assigned to you gets new commits, its session is asked
      to re-review what changed (when ``auto_review``).
    - When an agent wants something that isn't pre-approved, a
      :class:`PermissionRequest` goes out, the PR needs you, and the agent waits
      for a ``session.answer {request_id, allow}`` job.

    Stopping cancels running sessions; their reviews stay "running" in the
    store, which is how they get resumed next time.

    Args:
        name: Bus name.
        store: Where reviews, findings and actions are recorded.
        repos: Clones and worktrees.
        agent: Runs the agent sessions.
        github: Reads a PR's review threads for the agent (none: no threads).
        auto_review: Review PRs assigned to you without being asked.
        permission_wait: Seconds an agent waits for a permission answer before
            carrying on without it.
    """

    def __init__(
        self,
        name: str = REVIEW_WORKER,
        *,
        store: Store,
        repos: GitRepos,
        agent: ReviewAgent,
        github: GitHub | None = None,
        auto_review: bool = True,
        permission_wait: float = 300,
    ):
        super().__init__(name)
        self._store = store
        self._repos = repos
        self._agent = agent
        self._github = github
        self._permission_wait = permission_wait
        self._auto_review = auto_review
        self._running: dict[PRRef, asyncio.Task] = {}
        self._start_lock = asyncio.Lock()
        self._startup_task: asyncio.Task | None = None
        self._asks: dict[str, asyncio.Future[bool]] = {}

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
        # A follow-up cut short (Purr quit mid-turn) leaves its PR "reviewing".
        settled = False
        for pr in await self._store.list_prs(state=PRState.REVIEWING):
            review = await self._store.latest_review(pr.ref)
            if review is None or review.status != ReviewStatus.RUNNING:
                pending = await self._store.pending_actions(pr.ref)
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
                self._launch(pr, review, prompt=RESUME_PROMPT, resume=True)
        if self._auto_review:
            for pr in await self._store.list_prs(state=PRState.NOT_REVIEWED, open_only=True):
                if pr.assigned_to_me:
                    await self._try_start(pr.ref)

    async def on_bus_message(self, message: BusMessage) -> None:
        await super().on_bus_message(message)
        if not (self._auto_review and isinstance(message, PRChanged)):
            return
        if message.change == PRChange.ASSIGNED:
            pr = await self._store.get_pr(PRRef.parse(message.pr))
            if pr and pr.state == PRState.NOT_REVIEWED:
                await self._try_start(pr.ref)
        elif message.change == PRChange.NEW_COMMITS:
            await self.rereview(PRRef.parse(message.pr))

    async def rereview(self, ref: PRRef) -> bool:
        """Have a finished review look at the PR's new commits, in the same session.

        Returns whether a re-review started.
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
            prompt = rereview_prompt(review.head_sha, pr.head_sha)
            self._launch(pr, review, prompt=prompt, resume=True, followup=True)
        await self._progress(ref, "Re-reviewing new commits")
        return True

    async def _try_start(self, ref: PRRef) -> None:
        try:
            await self.start_review(ref)
        except ReviewError as e:
            logger.debug(f"Not starting a review of {ref}: {e}")

    # -- Jobs ---------------------------------------------------------------------

    @job(name=REVIEW_START)
    async def on_review_start(self, message: BusJobRequestMessage) -> None:
        try:
            review = await self.start_review(PRRef.parse(str((message.payload or {}).get("pr"))))
        except (ReviewError, ValueError) as e:
            await self.send_job_response(message.job_id, {"error": str(e)})
            return
        await self.send_job_response(message.job_id, {"review_id": review.id})

    @job(name=REVIEW_MESSAGE)
    async def on_review_message(self, message: BusJobRequestMessage) -> None:
        payload = message.payload or {}
        try:
            await self.send_followup(
                PRRef.parse(str(payload.get("pr"))), str(payload.get("text", ""))
            )
        except (ReviewError, ValueError) as e:
            await self.send_job_response(message.job_id, {"error": str(e)})
            return
        await self.send_job_response(message.job_id, {"ok": True})

    @job(name=PERMISSION_ANSWER)
    async def on_permission_answer(self, message: BusJobRequestMessage) -> None:
        payload = message.payload or {}
        future = self._asks.get(str(payload.get("request_id")))
        if future is None or future.done():
            await self.send_job_response(message.job_id, {"error": "Nothing is waiting for that"})
            return
        future.set_result(bool(payload.get("allow")))
        await self.send_job_response(message.job_id, {"ok": True})

    # -- Starting -------------------------------------------------------------------

    async def start_review(self, ref: PRRef) -> Review:
        """Start reviewing a PR in the background."""
        async with self._start_lock:
            if ref in self._running:
                raise ReviewError(f"{ref} is already being reviewed")
            pr = await self._store.get_pr(ref)
            if pr is None:
                raise ReviewError(f"Purr doesn't know {ref}")
            # A new review supersedes whatever the last one proposed.
            await self._dismiss_pending(ref)
            review = await self._store.start_review(
                ref, worktree_path=str(self._repos.worktree_path(ref)), head_sha=pr.head_sha
            )
            await self._store.set_review_session(review.id, str(uuid.uuid4()))
            review = await self._store.latest_review(ref)
            assert review is not None
            await self._store.set_pr_state(ref, PRState.REVIEWING)
            self._launch(pr, review, prompt=review_prompt(pr), resume=False)
        await self.send_bus_message(ReviewStarted(source=self.name, pr=str(ref)))
        await self.send_bus_message(PRListUpdated(source=self.name))
        return review

    async def send_followup(self, ref: PRRef, text: str) -> None:
        """Continue a PR's session with a message from the user."""
        if not text.strip():
            raise ReviewError("Nothing to send")
        async with self._start_lock:
            if ref in self._running:
                raise ReviewError(f"The agent is still working on {ref}")
            review = await self._store.latest_review(ref)
            pr = await self._store.get_pr(ref)
            if review is None or pr is None or not review.session_id:
                raise ReviewError(f"{ref} hasn't been reviewed yet")
            self._launch(pr, review, prompt=text, resume=True, followup=True)

    def _launch(self, pr: PR, review: Review, *, prompt: str, resume: bool, followup: bool = False):
        self._running[pr.ref] = self.create_task(
            self._run(pr, review, prompt=prompt, resume=resume, followup=followup),
            f"{self}::review::{pr.ref}",
        )

    # -- Running ----------------------------------------------------------------------

    async def _run(self, pr: PR, review: Review, *, prompt: str, resume: bool, followup: bool):
        ref = pr.ref
        submitted: dict = {}
        state_before = pr.state
        try:
            if followup:
                await self._store.set_pr_state(ref, PRState.REVIEWING)
                await self.send_bus_message(PRListUpdated(source=self.name))
            await self._progress(ref, "Checking out the PR")
            worktree = await self._repos.checkout(ref, pr.head_sha)
            assert review.session_id
            if resume and not self._agent.session_exists(review.session_id, worktree):
                # It never really started: begin again, under the same session ID.
                resume, prompt = False, review_prompt(pr)
            await self._progress(ref, "Reviewing" if not followup else "Thinking")
            result = await self._agent.run(
                pr=pr,
                worktree=worktree,
                session_id=review.session_id,
                resume=resume,
                prompt=prompt,
                callbacks=self._callbacks(ref, submitted),
            )
            if result.is_error:
                raise ReviewError(result.error or "The agent session failed")
            if submitted or not followup:
                summary = submitted.get("summary") or result.text or "No summary."
                findings: list[Finding] = submitted.get("findings", [])
                await self._store.finish_review(
                    review.id, summary=summary, findings=findings, head_sha=pr.head_sha
                )
                await self.send_bus_message(
                    ReviewFinished(
                        source=self.name,
                        pr=str(ref),
                        summary=summary,
                        findings=[asdict(f) for f in findings],
                    )
                )
            await self._settle(ref, state_before if followup else PRState.NEEDS_YOU)
        except asyncio.CancelledError:
            raise  # Purr is stopping; the review stays "running" and resumes next time
        except Exception as e:
            logger.exception(f"Review of {ref} failed")
            if followup:
                await self.send_bus_message(
                    SessionMessage(
                        source=self.name, pr=str(ref), message={"kind": "error", "text": str(e)}
                    )
                )
                await self._settle(ref, state_before)
            else:
                await self._store.fail_review(review.id, error=str(e))
                await self.send_bus_message(
                    ReviewFailed(source=self.name, pr=str(ref), error=str(e))
                )
                await self._settle(ref, PRState.NEEDS_YOU)
        finally:
            self._running.pop(ref, None)

    async def _settle(self, ref: PRRef, state: PRState) -> None:
        """Set the PR's state after a run: anything pending means it needs you."""
        if await self._store.pending_actions(ref):
            state = PRState.NEEDS_YOU
        await self._store.set_pr_state(ref, state)
        await self.send_bus_message(PRListUpdated(source=self.name))

    async def _progress(self, ref: PRRef, text: str) -> None:
        await self.send_bus_message(ReviewProgress(source=self.name, pr=str(ref), text=text))

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

    def _callbacks(self, ref: PRRef, submitted: dict) -> ReviewCallbacks:
        async def on_message(entry: dict) -> None:
            await self.send_bus_message(
                SessionMessage(source=self.name, pr=str(ref), message=entry)
            )
            if entry["kind"] == "tool_use":
                await self._progress(ref, describe_tool_use(entry["name"], entry["input"]))
            elif entry["kind"] == "thinking":
                first_line = entry["text"].strip().splitlines()[0]
                await self._progress(ref, f"Thinking: {first_line}")

        async def on_submit(summary: str, findings: list[Finding]) -> None:
            submitted["summary"] = summary
            submitted["findings"] = findings

        async def on_propose(kind: ActionKind, description: str, payload: dict) -> str:
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

        async def on_ask(tool: str, tool_input: dict, reason: str) -> bool | None:
            # The agent waits for the user, for a while; meanwhile the PR needs them.
            # Claude Code itself gives up after about ten minutes, and after that
            # the session refuses everything, so Purr answers first.
            request_id = uuid.uuid4().hex
            description = describe_tool_use(tool, tool_input)
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._asks[request_id] = future
            await self._store.set_pr_state(ref, PRState.NEEDS_YOU)
            await self.send_bus_message(
                PermissionRequest(
                    source=self.name,
                    pr=str(ref),
                    request_id=request_id,
                    tool=tool,
                    description=description,
                    reason=reason,
                )
            )
            await self.send_bus_message(PRListUpdated(source=self.name))
            answer: bool | None
            try:
                answer = await asyncio.wait_for(asyncio.shield(future), self._permission_wait)
            except TimeoutError:
                answer = None
            finally:
                self._asks.pop(request_id, None)
            await self._store.set_pr_state(ref, PRState.REVIEWING)
            await self.send_bus_message(
                PermissionResolved(
                    source=self.name,
                    pr=str(ref),
                    request_id=request_id,
                    allowed=bool(answer),
                    answered=answer is not None,
                    description=description,
                )
            )
            await self.send_bus_message(PRListUpdated(source=self.name))
            return answer

        async def on_threads() -> list[dict]:
            if not self._github:
                return []
            try:
                return await self._github.review_threads(ref)
            except GitHubError as e:
                logger.warning(f"Couldn't read the review threads of {ref}: {e}")
                return []

        return ReviewCallbacks(
            on_message=on_message,
            on_submit=on_submit,
            on_propose=on_propose,
            on_ask=on_ask,
            on_threads=on_threads,
        )
