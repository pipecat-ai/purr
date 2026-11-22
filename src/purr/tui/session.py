"""The PR session pane: a Claude Code-style view of a PR's agent session."""

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from purr.jobs import PurrError
from purr.messages import (
    ListRepoSkills,
    PermissionRequest,
    PlanReviews,
    SendMessage,
    SetModel,
)
from purr.models import (
    PR,
    Depth,
    Outcome,
    PendingAction,
    PRDetails,
    PRRef,
    PRState,
    Reply,
    Review,
    ReviewStatus,
)
from purr.review.agent import INSTEAD, thought_headline
from purr.review.skills import RepoSkill
from purr.review.tools import PROPOSE_ACTION, PURR_TOOL_PREFIX, SUBMIT_REVIEW
from purr.tui.commands import (
    PURR_NAMES,
    Command,
    parse,
    render_cost,
    render_help,
    suggestions,
)
from purr.tui.overview import details_line, render_overview
from purr.tui.prompt import HistorySearchScreen, PromptArea
from purr.tui.theme import BLUE, CODE_THEME
from purr.tui.transcript import Transcript
from purr.tui.working import Working

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_SEVERITY_STYLE = {"issue": "bold red", "suggestion": "yellow", "nit": "dim"}
NUMBER_STYLE = f"bold {BLUE}"  # PR numbers, wherever they show
# A background task's event: its mark, what it says, its style.
_TASK_EVENTS = {
    "started": ("◷", "started", "dim"),
    "completed": ("✓", "done", "dim green"),
    "stopped": ("◼", "stopped", "dim"),
    "killed": ("◼", "stopped", "dim"),
}
# Absolute paths into a PR's worktree read better relative to it.
_WORKTREE_PREFIX = re.compile(r"\S*/worktrees/[^/\s]+/[^/\s]+/\d+/")


@dataclass
class SessionState:
    """What the UI keeps about a PR's session between redraws."""

    entries: list[dict] | None = None  # None until the transcript is loaded
    progress: str | None = None  # what the review's doing; None once nothing is
    waiting: bool = False  # the review waits on you (a question)
    since: float | None = None  # when the agent started working (monotonic)
    background: int = 0  # its tasks (agents, commands) still running in the background
    ask: PermissionRequest | None = None
    details_error: str | None = None  # why the description couldn't be loaded
    diff_error: str | None = None  # why the diff couldn't be

    @property
    def working(self) -> bool:
        """The agent's at it (not waiting on you, or for its turn): the spinners spin."""
        return bool(self.progress) and self.since is not None and not self.waiting and not self.ask


def _bullet(body: RenderableType, style: str = "") -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_row(Text("⏺", style=style or "white"), body)
    return grid


def _short(value: str, width: int = 100) -> str:
    value = _WORKTREE_PREFIX.sub("", value).replace("\n", " ⏎ ")
    return value if len(value) <= width else value[: width - 1] + "…"


_VERDICT = {"request_changes": "Request changes", "approve": "Approve", "comment": "Comment"}


_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def render_proposal(proposal: dict, signature: str = "") -> RenderableType:
    """A proposed review, in full: what would be posted if the user approves.

    ``signature`` is the small print Purr adds to the review's text and replies.
    """
    parts: list[RenderableType] = [
        Text(f"Proposed: {proposal.get('description', '')}", style="bold magenta")
    ]
    verdict = _VERDICT.get(str(proposal.get("kind")), str(proposal.get("kind", "")))
    parts.append(Text(f"{verdict}", style="magenta"))
    if body := str(proposal.get("body") or "").strip():
        parts.append(Markdown(body, code_theme=CODE_THEME))
    elif proposal.get("kind") == "approve" and not proposal.get("comments"):
        parts.append(Text("LGTM"))  # what an approval with nothing to say posts
    signature = _MARKDOWN_LINK.sub(r"\1", signature)  # as it reads on GitHub
    if signature:
        parts.append(Text(signature, style="dim italic"))
    for comment in proposal.get("comments") or []:
        line = Text("• ")
        line.append(f"{comment.get('path')}:{comment.get('line')}", style="cyan")
        line.append(f"  {comment.get('body', '')}")
        parts.append(line)
    for reply in proposal.get("replies") or []:
        line = Text("↳ reply in thread  ", style="dim")
        line.append(str(reply.get("body", "")))
        if signature:
            line.append(f"  {signature}", style="dim italic")
        parts.append(line)
    return Group(*parts)


def replied(replies: Sequence[Reply]) -> str:
    """'alice replied', 'alice and bob replied (3)'."""
    authors = list(dict.fromkeys(reply.author for reply in replies))
    who = authors[0] if len(authors) == 1 else f"{', '.join(authors[:-1])} and {authors[-1]}"
    return f"{who} replied" + (f" ({len(replies)})" if len(replies) > 1 else "")


def render_replies(replies: Sequence[Reply], shown: int = 3) -> Text:
    """What others said to you since your last word, the latest few, and what x does."""
    text = Text(f"{replied(replies)}:", style="bold")
    for reply in replies[-shown:]:
        where = f" · {reply.where}" if reply.where else ""
        body = " ".join(reply.body.split())
        text.append(f"\n  {reply.author}{where}: ", style="cyan")
        text.append(_short(body, 110))
    if len(replies) > shown:
        text.append(f"\n  and {len(replies) - shown} before", style="dim")
    text.append("\n[x] no reply needed", style="bold")
    text.append("   or answer: here (ask the agent) or on GitHub", style="dim")
    return text


def outcome_mark(outcome: Outcome) -> Text:
    """What a review found, at a glance: ✓ nothing (nits don't count), ● issues."""
    return Text("●", style="bold red") if outcome.found else Text("✓", style="bold green")


def render_entry(
    entry: dict, signature: str = "", *, full_thinking: bool = False
) -> RenderableType | None:
    """A transcript entry as Claude Code would show it.

    Subagents' own steps aren't shown, only the call that started each one and
    what it came back with (they're recorded all the same). Each thought is a
    line, unless ``full_thinking``.
    """
    if entry.get("agent"):
        return None
    kind = entry.get("kind")
    if kind == "text":
        return _bullet(Markdown(entry["text"], code_theme=CODE_THEME))
    if kind == "thinking":
        # Like Claude Code: the reasoning, dimmed, ahead of what it leads to. In
        # full it's ten times what the agent says, so a line each unless you ask (t).
        text = entry["text"].strip() if full_thinking else thought_headline(entry["text"])
        return _bullet(Text(text, style="dim italic"), style="dim")
    if kind == "user":
        return Text(f"> {entry['text'].replace(chr(10), chr(10) + '  ')}", style="bold")
    if kind == "set_aside":  # you wrote instead of approving it
        return Text(f"  ◼ Proposal set aside: {_short(entry.get('text', ''))}", style="dim")
    if kind == "tool_use":
        name, tool_input = entry.get("name", ""), entry.get("input") or {}
        if name == PURR_TOOL_PREFIX + SUBMIT_REVIEW:
            lines = [Text(tool_input.get("summary", ""), style="bold")]
            for i, finding in enumerate(tool_input.get("findings") or [], start=1):
                severity = finding.get("severity", "suggestion")
                where = finding.get("file") or ""
                if where and finding.get("line"):
                    where += f":{finding['line']}"
                line = Text(f"{i}. ")
                line.append(severity, style=_SEVERITY_STYLE.get(severity, ""))
                if where:
                    line.append(f"  {where}", style="cyan")
                line.append(f"  {finding.get('text', '')}")
                lines.append(line)
            return _bullet(Group(*lines), style="bold green")
        if name == PURR_TOOL_PREFIX + PROPOSE_ACTION:
            return _bullet(render_proposal(tool_input, signature), style="magenta")
        target = (
            tool_input.get("command")
            or tool_input.get("file_path")
            or tool_input.get("pattern")
            or tool_input.get("path")
            or tool_input.get("url")
            or tool_input.get("description")  # a subagent's task
            or ""
        )
        line = Text(name.removeprefix(PURR_TOOL_PREFIX), style="bold")
        if target:
            line.append(f"({_short(str(target))})", style="dim")
        return _bullet(line, style="green")
    if kind == "tool_result":
        text = entry.get("text") or ""
        if text.startswith(("Review recorded", "Proposed (action")):
            return None  # Purr's own tools; their calls already say it all
        if text.startswith(INSTEAD):  # you wrote instead of allowing it
            said = text.removeprefix(INSTEAD).strip()
            return Group(
                Text("  ⎿ Not allowed: you wrote instead", style="dim"),
                Text(""),
                render_entry({"kind": "user", "text": said}),
            )
        lines = text.strip().splitlines() or [""]
        more = f" … +{len(lines) - 1} lines" if len(lines) > 1 else ""
        style = "red" if entry.get("is_error") else "dim"
        return Text(f"  ⎿ {_short(lines[0], 90)}{more}", style=style)
    if kind == "result":
        turns, cost = entry.get("turns") or 0, entry.get("cost_usd")
        details = [f"{turns} turn{'s' if turns != 1 else ''}"] if turns else []
        details += [f"${cost:.2f}"] if cost else []
        return Text("  ✓ done" + "".join(f" · {d}" for d in details), style="dim")
    if kind == "error":
        return Text(f"✗ {entry.get('text', '')}", style="red")
    if kind == "task":  # a background agent (or command) starting or reporting back
        # Stopped isn't failed: the agent stops what it no longer needs.
        mark, verb, style = _TASK_EVENTS.get(entry.get("event"), ("✗", "failed", "dim red"))
        return Text(
            f"  {mark} background {verb}: {_short(entry.get('text', ''), 100)}", style=style
        )
    if kind == "permission_auto":
        return Text(f"  ✓ allowed by your permission {entry.get('rule', '')}", style="dim green")
    return None


class PRSession(Vertical):
    """Header, transcript, the decision box, and the prompt for one PR.

    The transcript opens with the PR's overview (description and files), so a
    PR nobody has reviewed yet shows what it's about, and a reviewed one keeps
    it above the session.
    """

    BINDINGS = [
        Binding("enter", "ask", "Ask"),
        # The transcript, wherever the focus is in the pane, typing included.
        Binding("pageup", "page(-1)", "Page up", show=False, priority=True),
        Binding("pagedown", "page(1)", "Page down", show=False, priority=True),
        Binding("ctrl+r", "search_history", "Search history", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pr: PR | None = None

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def compose(self) -> ComposeResult:
        yield Static(id="pr-header")
        yield Transcript(id="transcript", wrap=True, markup=False, auto_scroll=True)
        yield Working(id="working")
        yield Static(id="pr-decision")
        suggestions = OptionList(id="suggestions")
        suggestions.can_focus = False
        yield suggestions
        yield PromptArea(
            id="prompt", placeholder="Ask the agent about this PR…  (/ commands, ctrl+j: new line)"
        )

    def on_mount(self) -> None:
        self.border_title = "PR"
        self.query_one("#pr-decision").display = False
        self.query_one("#suggestions").display = False
        self.query_one(PromptArea).completer = self
        self._suggested: list[Command] = []
        self._drawn: PRRef | None = None  # the PR whose transcript is on screen
        self._wrote = False  # anything in the transcript yet (for the blank lines)

    # -- Slash commands (see purr.tui.commands) ----------------------------------------

    def _skills(self) -> list[RepoSkill]:
        """The repo's skills, for suggestions; fetched in the background the first time."""
        if not self.pr:
            return []
        repo = self.pr.repo
        cache = self.purr_app.repo_skills
        if repo not in cache:
            cache[repo] = []
            self.run_worker(self._load_skills(repo), group="skills")
        return cache[repo]

    async def _load_skills(self, repo: str) -> list[RepoSkill]:
        try:
            reply = await self.purr_app.bus.request(ListRepoSkills(repo=repo))
            self.purr_app.repo_skills[repo] = reply.skills
        except PurrError:
            self.purr_app.repo_skills.pop(repo, None)  # try again next time
            return []
        self._suggest()
        return self.purr_app.repo_skills[repo]

    @on(TextArea.Changed, "#prompt")
    def _suggest(self) -> None:
        box = self.query_one("#suggestions", OptionList)
        self._suggested = suggestions(self.query_one(PromptArea).text, self._skills())
        box.display = bool(self._suggested)
        if self._suggested:
            box.set_options(
                [
                    Option(
                        Text.assemble(
                            (f"/{c.name}", "bold"), (f"  {c.args}", ""), (f"  {c.help}", "dim")
                        )
                    )
                    for c in self._suggested
                ]
            )
            box.highlighted = 0

    def active(self) -> bool:
        return bool(self._suggested) and self.query_one("#suggestions").display

    def close_suggestions(self) -> bool:
        """Close the / suggestions, if they're open (esc)."""
        if not self.active():
            return False
        self.query_one("#suggestions").display = False
        self._suggested = []
        return True

    def move(self, step: int) -> None:
        box = self.query_one("#suggestions", OptionList)
        box.action_cursor_down() if step > 0 else box.action_cursor_up()

    def accept(self) -> str | None:
        index = self.query_one("#suggestions", OptionList).highlighted
        if index is None or index >= len(self._suggested):
            return None
        return f"/{self._suggested[index].name}"

    async def _command(self, name: str, args: str) -> None:
        """Run one of Purr's commands; its output goes in the transcript, just for you."""
        assert self.pr
        log, ref = self.query_one(RichLog), self.pr.ref
        log.write(Text(f"\n> /{name} {args}".rstrip(), style="bold"))
        if name == "help":
            log.write(render_help(await self._load_skills(ref.repo)))
        elif name in ("review", "clear"):
            if args not in ("", "quick", "full"):
                log.write(Text("Say /review, /review quick or /review full.", style="yellow"))
                return
            depth = Depth(args) if args else None
            errors = (await self.purr_app.bus.request(PlanReviews(prs=[ref], depth=depth))).errors
            for error in errors.values():
                log.write(Text(error, style="yellow"))
            if not errors:
                log.write(Text("Starting over with a fresh review.", style="dim"))
        elif name == "model":
            if args:
                model = None if args in ("default", "auto") else args
                await self.purr_app.bus.request(SetModel(pr=ref, model=model))
                self.pr = await self.purr_app.purr.store.get_pr(ref) or self.pr
            chosen = self.pr.model
            config = self.purr_app.purr.config
            text = (
                f"This PR's conversation uses {chosen}."
                if chosen
                else f"Purr's choice: {config.review_model or 'Claude Code’s default'}, and "
                f"{config.quick_model or 'the same'} for quick reviews. /model <name> to choose."
            )
            log.write(Text(text, style="dim"))
        elif name == "cost":
            log.write(render_cost(await self.purr_app.purr.store.runs(ref)))
        elif name == "shell":
            await self.purr_app.main.repo.action_shell()
        elif name == "claude":
            await self.purr_app.main.repo.action_open_claude()

    def state(self, pr: PR | None = None) -> SessionState:
        pr = pr or self.pr
        assert pr is not None
        return self.purr_app.sessions.setdefault(str(pr.ref), SessionState())

    async def show(self, pr: PR) -> None:
        """Show a PR (or refresh the one shown).

        Redraws when the transcript shows anything else: going by what was last
        drawn rather than what was last asked for, a show cut short (moving on
        quickly, switching repos) can't leave another PR's transcript behind.
        """
        self.pr = pr
        await self.refresh_status()
        state = self.state()
        loaded = state.entries is None
        if loaded:
            state.entries = await self._load_history(pr)
        if loaded or self._drawn != pr.ref:
            self.redraw()
            asked = await self.purr_app.purr.store.prompts(pr.ref)
            self.query_one(PromptArea).set_history([text for _, text in reversed(asked)])

    def redraw(self, *, keep_place: bool = False) -> None:
        """Rewrite the transcript: the overview, then the session.

        ``keep_place``: stay about as far down it as you were, rather than at its end.
        """
        assert self.pr is not None
        state = self.state()
        log = self.query_one(RichLog)
        place = log.scroll_y / log.max_scroll_y if log.max_scroll_y else 1.0
        log.clear()
        details = self.purr_app.details.get(self.pr)
        diff = self.purr_app.details.diff(self.pr)
        overview = render_overview(details, state.details_error, diff, state.diff_error)
        log.write(overview, scroll_end=False)
        self._wrote = True
        for entry in state.entries or []:
            self._write(entry)
        if not state.entries:
            log.scroll_home(animate=False)
        elif keep_place and place < 1:
            # After the writes' own scrolling to the end.
            log.call_after_refresh(
                lambda: log.scroll_to(y=round(place * log.max_scroll_y), animate=False)
            )
        self._drawn = self.pr.ref

    async def details_changed(self) -> None:
        """The shown PR's details arrived (or failed to)."""
        await self.refresh_status()
        self.redraw()

    async def _load_history(self, pr: PR) -> list[dict]:
        review = await self.purr_app.purr.store.latest_review(pr.ref)
        if not review or not review.session_id or not review.worktree_path:
            return []
        from pathlib import Path

        agent = self.purr_app.purr.agent
        try:
            return await asyncio.to_thread(
                agent.history, review.session_id, Path(review.worktree_path)
            )
        except Exception:
            return []

    def append(self, pr_key: str, entry: dict) -> None:
        """A live transcript entry for some PR; drawn if it's the one shown."""
        state = self.purr_app.sessions.setdefault(pr_key, SessionState())
        if state.entries is None:
            return  # not loaded yet; it'll come with the history
        state.entries.append(entry)
        if self._drawn and str(self._drawn) == pr_key:
            self._write(entry)

    def _write(self, entry: dict) -> None:
        """One transcript entry, a blank line apart from the one before it.

        Like Claude Code, a tool call and its result are one step: no line between.
        """
        app = self.purr_app
        signature = app.purr.config.signature
        if (renderable := render_entry(entry, signature, full_thinking=app.full_thinking)) is None:
            return
        log = self.query_one(RichLog)
        if self._wrote and entry.get("kind") != "tool_result":
            log.write(Text(""))
        log.write(renderable)
        self._wrote = True

    async def refresh_status(self) -> None:
        if not self.pr:
            return
        store = self.purr_app.purr.store
        pr = await store.get_pr(self.pr.ref) or self.pr
        self.pr = pr
        review = await store.latest_review(pr.ref)
        outcome = Outcome.of(await store.findings(review.id)) if review else Outcome()
        actions = await store.pending_actions(pr.ref)
        state = self.state()
        details = self.purr_app.details.get(pr)
        self.query_one("#pr-header", Static).update(
            self._header(pr, review, outcome, actions, state, details)
        )
        working = self.query_one(Working)
        if state.working:
            assert state.since is not None
            working.start(state.since, state.background)
        else:
            working.stop()
        decision = self.query_one("#pr-decision", Static)
        if state.ask:
            always = "   [A] always…" if state.ask.rule else ""
            decision.update(
                Text.assemble(
                    ("The agent wants to: ", "bold"),
                    (_short(state.ask.description, 120), "yellow"),
                    ("\n", ""),
                    (state.ask.reason or "Claude Code asks before this.", "dim"),
                    (f"\n[a] allow once{always}   [x] deny", "bold"),
                    ("   or write what to do instead", "dim"),
                )
            )
            decision.display = True
        elif actions:
            action = actions[0]
            comments = len(action.payload.get("comments") or [])
            plural = "s" if comments != 1 else ""
            detail = f" · {comments} inline comment{plural}" if comments else ""
            decision.update(
                Text.assemble(
                    (f"{action.description}?", "bold"),
                    (detail, "dim"),
                    ("\n[a] approve   [x] dismiss", "bold"),
                    ("   or write to change it", "dim"),
                )
            )
            decision.display = True
        elif pr.replies:
            decision.update(render_replies(pr.replies))
            decision.display = True
        else:
            decision.display = False

    @staticmethod
    def _header(
        pr: PR,
        review: Review | None,
        outcome: Outcome,
        actions: list[PendingAction],
        state: SessionState,
        details: PRDetails | None,
    ) -> Text:
        text = Text()
        text.append(f"#{pr.number}", style=NUMBER_STYLE)
        text.append(f"  {pr.title}\n", style="bold")
        text.append(details_line(pr, details))
        text.append("\n")
        if state.ask:
            text.append("⚠ Waiting for you to allow something", style="bold yellow")
        elif pr.state == PRState.REVIEWING:
            text.append("⟳ Reviewing", style="bold cyan")
            if state.progress:
                text.append(f" · {_short(state.progress, 80)}", style="dim")
        elif state.progress:  # the lead is planning its review
            text.append(f"⟳ {_short(state.progress, 80)}", style="bold cyan")
        elif review and review.status == ReviewStatus.FAILED:
            text.append(f"✗ Review failed: {review.error}", style="bold red")
        elif review and review.status == ReviewStatus.FINISHED:
            mark = outcome_mark(outcome)
            text.append(mark)
            text.append(f" Review: {outcome.text()}", style=mark.style)
            if actions:
                text.append(f" · {len(actions)} pending action", style="yellow")
        elif review is None and pr.others_on_it:
            text.append("◌ Not reviewed by you", style="bold")
            if pr.requested_from_me:
                text.append(" · asks for your review", style="magenta")
            text.append(" · r to review it too · d diff", style="dim")
        elif review is None:
            text.append("○ Not reviewed", style="bold")
            if pr.requested_from_me:
                text.append(" · asks for your review", style="magenta")
            hint = " · r to review it" + ("" if pr.assigned_to_me else " and take it")
            text.append(f"{hint} · d diff", style="dim")
        if pr.replies and review is not None:
            text.append(f" · {replied(pr.replies)}", style="yellow")
        if review and review.worktree_path:
            path = Path(review.worktree_path)
            home = str(Path.home())
            shown = "~" + str(path)[len(home) :] if str(path).startswith(home) else str(path)
            where = "! opens a shell there" if path.exists() else "removed; ! makes it again"
            text.append(f"\nWorktree {shown} · {where}", style="dim")
        if review and review.plan and pr.state != PRState.NOT_REVIEWED:
            # What the lead planned, and why.
            plan = review.plan
            line = f"{plan.depth.value.capitalize()} review · {plan.priority.value} priority"
            text.append(f"\n{line}" + (f" · {plan.why}" if plan.why else ""), style="dim")
        return text

    def focus_prompt(self) -> None:
        self.query_one(PromptArea).focus()

    def focus_transcript(self) -> None:
        self.query_one("#transcript").focus()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Typing a question, enter sends it.
        return not (action == "ask" and self.query_one("#prompt").has_focus)

    def action_ask(self) -> None:
        self.focus_prompt()

    def action_page(self, step: int) -> None:
        log = self.query_one(RichLog)
        log.scroll_page_down() if step > 0 else log.scroll_page_up()

    async def action_search_history(self) -> None:
        prompt = self.query_one(PromptArea)
        asked = await self.purr_app.purr.store.prompts()

        def picked(text: str | None) -> None:
            if text is not None:
                prompt.set_text(text)
            prompt.focus()

        self.app.push_screen(HistorySearchScreen(asked, prompt.text), picked)

    @on(PromptArea.Submitted)
    async def send(self, event: PromptArea.Submitted) -> None:
        text = event.text.strip()
        if not text or not self.pr:
            return
        command = parse(text)
        if command and command[0] in PURR_NAMES:
            event.prompt.clear()
            event.prompt.remember(text)
            try:
                await self._command(*command)
            except PurrError as e:
                self.notify(str(e), severity="warning")
            return
        try:
            sent = await self.purr_app.bus.request(SendMessage(pr=self.pr.ref, text=text))
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        event.prompt.clear()
        event.prompt.remember(text)
        ref = str(self.pr.ref)
        for proposal in sent.set_aside:
            self.append(ref, {"kind": "set_aside", "text": proposal})
        if not sent.answered:  # if it did, it shows under the call it said no to
            self.append(ref, {"kind": "user", "text": text})
