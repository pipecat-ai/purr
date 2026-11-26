"""What the repo page's keys do to the selected PR (or the marked ones): review it,
post or dismiss what's proposed, answer the agent, merge, and open it outside Purr."""

import asyncio
import os
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from textual import work

from purr.claude import NO_CLAUDE_CODE, claude_cli
from purr.jobs import PurrError
from purr.messages import (
    REVIEW_WORKER,
    AnswerPermission,
    ApproveAction,
    ApprovePR,
    AssignMe,
    CheckMerge,
    DismissAction,
    DismissReplies,
    GetWorktree,
    MergePR,
    MergeReadiness,
    PlanReviews,
    PostComments,
    StopAgent,
)
from purr.models import PR, ActionKind, CheckState, PRRef, ReviewStatus
from purr.review.agent import claude_code_command
from purr.tui.dialogs import AlwaysScreen, ConfirmScreen, NoticeScreen
from purr.tui.session import PRSession, SessionState
from purr.tui.views import PurrView


def _review_anyway(taken: list[PR]) -> ConfirmScreen:
    """Ask before reviewing PRs someone else is already on."""
    who = ", ".join(f"#{pr.number} ({', '.join(pr.assignees)})" for pr in taken)
    return ConfirmScreen(
        f"Someone's already on {who}. Review anyway?", "You'd be added as an assignee too."
    )


def _review_again(done: list[PR]) -> ConfirmScreen:
    """Ask before reviewing PRs again from the start."""
    which = " and ".join(f"#{pr.number}" for pr in done) if len(done) < 4 else f"{len(done)} PRs"
    were = "was" if len(done) == 1 else "were"
    return ConfirmScreen(
        f"{which} {were} reviewed already. Review again, from the start?",
        "What was proposed is set aside. esc while it works stops it, and brings it back.",
    )


def _shell() -> str:
    """Your shell: $SHELL, or Windows' (%COMSPEC%), or sh."""
    if shell := os.environ.get("SHELL"):
        return shell
    if os.name == "nt":
        return os.environ.get("COMSPEC") or "cmd.exe"
    return "/bin/sh"


MERGE_RECHECK = 5.0  # seconds between asking GitHub again while checks run

# What the spinner says while a does what's proposed ("Posting" for the rest).
_DOING = {ActionKind.PUSH: "Pushing", ActionKind.APPROVE: "Approving", ActionKind.MERGE: "Merging"}


def _checks_running(merge: MergeReadiness | None) -> bool:
    return bool(merge) and any(c.state == CheckState.RUNNING for c in merge.check.checks)


class PRKeys(PurrView):
    """The keys' side of the repo page (RepoView), which says what's selected and marked."""

    _prs: dict[str, PR]  # the listed PRs, by "owner/repo#123"
    _marked: set[str]  # the ones marked (space) to review together

    def selected_pr(self) -> PR | None:
        raise NotImplementedError

    def zoom_in_on_pr(self) -> None:
        raise NotImplementedError

    def working_changed(self) -> None:
        raise NotImplementedError

    @property
    def session(self) -> PRSession:
        return self.query_one(PRSession)

    @asynccontextmanager
    async def _doing(self, pr: PR, what: str) -> AsyncIterator[None]:
        """What you asked for (``Pushing``) moves at once, in the decision box's place and
        as the PR's mark in the list, till it's done: git and GitHub take a few seconds."""
        state = self.session.state(pr)
        state.doing, state.doing_since = what, time.monotonic()
        self.session.show_doing()
        self.working_changed()
        try:
            yield
        finally:
            state.doing = ""
            self.working_changed()
            await self.session.refresh_status()

    # -- Reviewing -----------------------------------------------------------------

    @work(group="keys")
    async def action_review(self) -> None:
        """Review the marked PRs, or else the selected one."""
        targets = [self._prs[ref] for ref in self._marked if ref in self._prs]
        if not targets and (pr := self.selected_pr()):
            targets = [pr]
        taken = [pr for pr in targets if pr.others_on_it]
        if taken and not await self.app.push_screen_wait(_review_anyway(taken)):
            return
        done = [pr for pr in targets if await self._reviewed(pr)]
        if done and not await self.app.push_screen_wait(_review_again(done)):
            return
        self._marked.clear()
        self.refresh_bindings()
        if not targets:
            return
        if self.session.pr and [pr.ref for pr in targets] == [self.session.pr.ref]:
            self.zoom_in_on_pr()  # to follow it; z or esc comes back
        plural = "s" if len(targets) != 1 else ""
        self.notify(f"Planning {len(targets)} review{plural}")
        # The lead plans them together (a few seconds), then they start. Meanwhile they
        # show as reviewing; you're assigned the ones that started.
        try:
            planned = await self.purr_app.bus.request(PlanReviews(prs=[pr.ref for pr in targets]))
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        for error in planned.errors.values():
            self.notify(error, severity="warning")
        await self._take([pr for pr in targets if pr.ref in planned.started])

    async def _reviewed(self, pr: PR) -> bool:
        """Whether the PR's latest review finished: r starts it over."""
        review = await self.purr_app.purr.store.latest_review(pr.ref)
        return bool(review and review.status == ReviewStatus.FINISHED)

    def at_work(self) -> PR | None:
        """The PR in view, if its agent's doing something: reviewing (or waiting its turn,
        or for you), answering you, or the lead planning its review."""
        pr = self.session.pr
        state = self.purr_app.sessions.get(str(pr.ref)) if pr else None
        return pr if state and state.progress else None

    @work(group="stop", exclusive=True)
    async def action_stop(self) -> None:
        """Stop what the agent's doing on the PR in view (esc), as in Claude Code."""
        if not (pr := self.at_work()):
            return
        try:
            await self.purr_app.bus.request(StopAgent(pr=pr.ref))
        except PurrError as e:
            self.notify(str(e), severity="warning")

    async def _take(self, prs: list[PR]) -> None:
        """Taking PRs from the pool: assign yourself, so teammates see they're taken (yours
        are already yours)."""
        for pr in prs:
            if pr.assigned_to_me or pr.authored:
                continue
            try:
                await self.purr_app.bus.request(AssignMe(pr=pr.ref))
            except PurrError as e:
                self.notify(str(e), severity="warning")

    # -- What's proposed, and what the agent asks ----------------------------------

    def _asking(self) -> SessionState | None:
        """The shown PR's session, if its agent is waiting on a permission answer."""
        pr = self.selected_pr()
        state = self.purr_app.sessions.get(str(pr.ref)) if pr else None
        return state if state and state.ask else None

    @work(group="keys")
    async def action_approve(self) -> None:
        if state := self._asking():
            await self._answer(state, True)
            return
        pr = self.selected_pr()
        if not pr or self.session.state(pr).doing or not (to_do := await self._to_post(pr)):
            return
        job, doing, done = to_do
        async with self._doing(pr, doing):
            try:
                posted = await self.purr_app.bus.request(job)
            except PurrError as e:
                self.notify(str(e), severity="error")
                return
        self.notify(f"{done}: {posted.url}" if posted.url else done)
        for warning in posted.warnings:
            self.notify(warning, severity="warning")

    async def _to_post(
        self, pr: PR
    ) -> tuple[ApproveAction | PostComments | ApprovePR, str, str] | None:
        """What a does, what the spinner says meanwhile, and what it says once done: the
        agent's proposal (a review, with your comments, or a push), your comments on their
        own, or, waiting for your verdict alone, approving the PR."""
        store = self.purr_app.purr.store
        if actions := await store.pending_actions(pr.ref):
            kind = actions[0].kind
            done = "Pushed" if kind == ActionKind.PUSH else "Posted"
            return ApproveAction(action_id=actions[0].id), _DOING.get(kind, "Posting"), done
        if await store.draft_comments(pr.ref):
            return PostComments(pr=pr.ref), "Posting", "Posted"
        state = self.purr_app.sessions.get(str(pr.ref))
        verdict = state and state.verdict_due
        return (ApprovePR(pr=pr.ref), "Approving", "Approved") if verdict else None

    @work(group="keys")
    async def action_dismiss(self) -> None:
        if state := self._asking():
            await self._answer(state, False)
            return
        pr = self.selected_pr()
        if pr is None or self.session.state(pr).doing:  # it's being done: too late
            return
        state = self.session.state(pr)
        if state.merge:
            state.merge = None  # the merge box goes: m asks again
            await self.session.refresh_status()
            return
        if state.progress:  # the agent's at it: the box waits, and so does x
            return
        store = self.purr_app.purr.store
        actions = await store.pending_actions(pr.ref)
        try:
            if actions:
                await self.purr_app.bus.request(DismissAction(action_id=actions[0].id))
            elif (current := await store.get_pr(pr.ref)) and current.replies:
                await self.purr_app.bus.request(DismissReplies(pr=pr.ref))  # no reply needed
        except PurrError as e:
            self.notify(str(e), severity="warning")

    @work(group="keys")
    async def action_always(self) -> None:
        """Allow what the agent asks, and from now on: in this repo or everywhere."""
        state = self._asking()
        if not state or not state.ask or not state.ask.rule:
            return
        ask = state.ask
        repo = PRRef.parse(ask.pr).repo
        scope = await self.app.push_screen_wait(AlwaysScreen(ask.rule, repo))
        if not scope:
            return
        try:
            answer = AnswerPermission(request_id=ask.request_id, allow=True, remember=scope)
            rule = (await self.purr_app.bus.request(answer)).rule
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        where = f"in {repo}" if scope == "repo" else "everywhere"
        self.notify(f"Allowed from now on, {where}: {rule}")

    async def _answer(self, state: SessionState, allow: bool) -> None:
        assert state.ask
        try:
            answer = AnswerPermission(request_id=state.ask.request_id, allow=allow)
            await self.purr_app.bus.request(answer)
        except PurrError as e:
            self.notify(str(e), severity="warning")

    # -- Merging -------------------------------------------------------------------

    def _leave_merge(self, left: str | None, shown: str) -> None:
        """Moving on from a PR drops where it stood for merging: m asks again next time."""
        if left and left != shown and (state := self.purr_app.sessions.get(left)):
            state.merge = None

    @work(group="keys")
    async def action_merge(self) -> None:
        """m: where the PR stands for merging; m again, with nothing blocking it: merge."""
        pr = self.selected_pr()
        if pr is None or (state := self.session.state(pr)).doing:
            return
        merging = state.merge is not None and not state.merge.blockers
        async with self._doing(pr, "Merging" if merging else "Looking at its checks and reviews"):
            try:
                await (self._merge(pr, state) if merging else self._check_merge(pr, state))
            except PurrError as e:
                state.merge = None
                self.notify(str(e), severity="warning")
        self._follow_checks(pr, state)

    async def _check_merge(self, pr: PR, state: SessionState) -> None:
        """Where it stands for merging, fresh from GitHub: if nothing changed, it says so."""
        before = state.merge
        state.merge = await self.purr_app.bus.request(CheckMerge(pr=pr.ref))
        if before and state.merge.blockers == before.blockers:
            self.notify(f"Still can't merge: {before.blockers[0]}", severity="warning")

    @work(exclusive=True, group="merge-checks")
    async def _follow_checks(self, pr: PR, state: SessionState) -> None:
        """While the merge box says a check is still running, it stays current: GitHub's
        asked again every MERGE_RECHECK seconds until none is, or the box is gone."""
        while _checks_running(state.merge):
            await asyncio.sleep(MERGE_RECHECK)
            if not _checks_running(state.merge):
                return  # merged, or you moved on, meanwhile
            try:
                state.merge = await self.purr_app.bus.request(CheckMerge(pr=pr.ref))
            except PurrError:
                return  # what it said stays; m asks again
            await self.session.refresh_status()

    async def _merge(self, pr: PR, state: SessionState) -> None:
        """Merge past what you saw, unless something new came up: then you see it again."""
        assert state.merge is not None
        request = MergePR(pr=pr.ref, seen=state.merge.concerns)
        merged = await self.purr_app.bus.request(request)
        state.merge = merged.readiness
        if merged.merged:
            self.notify(f"Merged #{pr.number} into {pr.base or 'its base'}")
        else:
            self.notify("Something changed since you looked: see it again", severity="warning")

    # -- Outside Purr --------------------------------------------------------------

    async def action_open_claude(self) -> None:
        pr = self.selected_pr()
        if not pr:
            return
        review = await self.purr_app.purr.store.latest_review(pr.ref)
        if not review or not review.session_id or not review.worktree_path:
            self.notify("No session yet. Press r to review it first.")
            return
        reviews = self.purr_app.purr.runner.get_worker(REVIEW_WORKER)
        if reviews and pr.ref in getattr(reviews, "running", set()):
            self.notify("The agent is still working on it. Open it once it's done.")
            return
        # Its worktree may have been removed (approved, say): it comes back as it was.
        try:
            worktree = (await self.purr_app.bus.request(GetWorktree(pr=pr.ref))).path
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        if not (cli := claude_cli()):
            self.app.push_screen(NoticeScreen("There's no Claude Code to open.", NO_CLAUDE_CODE))
            return
        claude = claude_code_command(review.session_id, str(pr.ref), cli)
        banner = f"Claude Code, in {pr.ref}'s review session. /exit goes back to Purr."
        if await self.purr_app.run_outside(claude, worktree, banner=banner) is None:
            self.app.push_screen(NoticeScreen(f"Claude Code didn't start ({cli}).", NO_CLAUDE_CODE))
            return
        # The session may have grown in Claude Code: reload its transcript.
        self.purr_app.sessions.pop(str(pr.ref), None)
        self.session.pr = None
        await self.session.show(pr)

    async def action_shell(self) -> None:
        """A shell in the PR's worktree, to run or change things yourself; exit to come back.

        The worktree is made if the PR doesn't have one (never reviewed, or
        removed once approved).
        """
        pr = self.selected_pr()
        if not pr:
            return
        try:
            worktree = (await self.purr_app.bus.request(GetWorktree(pr=pr.ref))).path
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        banner = f"{pr.ref} at {pr.head_sha[:7]}, in {worktree}. Exit the shell to go back to Purr."
        env = {"PURR_PR": str(pr.ref)}
        await self.purr_app.run_outside([_shell()], worktree, env, banner=banner)
        self.purr_app.data_version += 1  # its header says whether it has a worktree now
        await self.session.refresh_status()

    async def action_pager(self) -> None:
        """The PR's full diff in your pager (gh's pager settings, or less)."""
        pr = self.selected_pr()
        if not pr:
            return
        env = dict(os.environ)
        if shutil.which("less"):  # gh's own choice otherwise (Windows has none)
            env.setdefault("PAGER", "less")
        # gh's default LESS=FRX quits at once on a diff that fits the screen,
        # which would hand the terminal straight back to Purr.
        env.setdefault("LESS", "R")
        command = ["gh", "pr", "diff", str(pr.number), "--repo", pr.repo]
        with self.app.suspend():
            process = await asyncio.create_subprocess_exec(*command, env=env)
            await process.wait()
