"""The PR session pane: a Claude Code-style view of a PR's agent session."""

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, RichLog, Static

from purr.messages import PermissionRequest
from purr.models import PR, PendingAction, PRState, Review, ReviewStatus
from purr.review.tools import PROPOSE_ACTION, PURR_TOOL_PREFIX, SUBMIT_REVIEW
from purr.tui.bridge import RequestError

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_SEVERITY_STYLE = {"issue": "bold red", "suggestion": "yellow", "nit": "dim"}
# Absolute paths into a PR's worktree read better relative to it.
_WORKTREE_PREFIX = re.compile(r"\S*/worktrees/[^/\s]+/[^/\s]+/\d+/")


@dataclass
class SessionState:
    """What the UI keeps about a PR's session between redraws."""

    entries: list[dict] | None = None  # None until the transcript is loaded
    progress: str | None = None
    ask: PermissionRequest | None = None


def _bullet(body: RenderableType, style: str = "") -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_row(Text("⏺", style=style or "white"), body)
    return grid


def _short(value: str, width: int = 100) -> str:
    value = _WORKTREE_PREFIX.sub("", value).replace("\n", " ⏎ ")
    return value if len(value) <= width else value[: width - 1] + "…"


def render_entry(entry: dict) -> RenderableType | None:
    """A transcript entry as Claude Code would show it."""
    kind = entry.get("kind")
    if kind == "text":
        return _bullet(Markdown(entry["text"]))
    if kind == "user":
        return Text(f"\n> {entry['text']}\n", style="bold")
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
            return _bullet(
                Text(f"Proposed: {tool_input.get('description', '')}", style="bold magenta"),
                style="magenta",
            )
        target = (
            tool_input.get("command")
            or tool_input.get("file_path")
            or tool_input.get("pattern")
            or tool_input.get("path")
            or tool_input.get("url")
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
        lines = text.strip().splitlines() or [""]
        more = f" … +{len(lines) - 1} lines" if len(lines) > 1 else ""
        style = "red" if entry.get("is_error") else "dim"
        return Text(f"  ⎿ {_short(lines[0], 90)}{more}", style=style)
    if kind == "result":
        cost = entry.get("cost_usd")
        details = f"{entry.get('turns', 0)} turns" + (f" · ${cost:.2f}" if cost else "")
        return Text(f"  ✓ done · {details}\n", style="dim")
    if kind == "error":
        return Text(f"✗ {entry.get('text', '')}", style="red")
    return None


class PRSession(Vertical):
    """Header, transcript, the decision box, and the prompt for one PR."""

    BINDINGS = [Binding("escape", "leave_prompt", "Back to the list", show=False)]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pr: PR | None = None

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def compose(self) -> ComposeResult:
        yield Static(id="pr-header")
        yield RichLog(id="transcript", wrap=True, markup=False, auto_scroll=True)
        yield Static(id="pr-decision")
        yield Input(id="prompt", placeholder="Ask the agent about this PR…")

    def on_mount(self) -> None:
        self.query_one("#pr-decision").display = False

    def state(self, pr: PR | None = None) -> SessionState:
        pr = pr or self.pr
        assert pr is not None
        return self.purr_app.sessions.setdefault(str(pr.ref), SessionState())

    async def show(self, pr: PR) -> None:
        """Show a PR (or refresh the one shown)."""
        switching = self.pr is None or self.pr.ref != pr.ref
        self.pr = pr
        await self.refresh_status()
        state = self.state()
        if state.entries is None:
            state.entries = await self._load_history(pr)
            switching = True
        if switching:
            log = self.query_one(RichLog)
            log.clear()
            for entry in state.entries:
                self._write(entry)

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
        if self.pr and str(self.pr.ref) == pr_key:
            self._write(entry)

    def _write(self, entry: dict) -> None:
        if (renderable := render_entry(entry)) is not None:
            self.query_one(RichLog).write(renderable)

    async def refresh_status(self) -> None:
        if not self.pr:
            return
        store = self.purr_app.purr.store
        pr = await store.get_pr(self.pr.ref) or self.pr
        self.pr = pr
        review = await store.latest_review(pr.ref)
        findings = len(await store.findings(review.id)) if review else 0
        actions = await store.pending_actions(pr.ref)
        state = self.state()
        self.query_one("#pr-header", Static).update(
            self._header(pr, review, findings, actions, state)
        )
        decision = self.query_one("#pr-decision", Static)
        if state.ask:
            decision.update(
                Text.assemble(
                    ("The agent wants to: ", "bold"),
                    (_short(state.ask.description, 120), "yellow"),
                    ("\n", ""),
                    (state.ask.reason or "It isn't pre-approved.", "dim"),
                    ("\n[y] allow   [n] deny", "bold"),
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
                )
            )
            decision.display = True
        else:
            decision.display = False

    @staticmethod
    def _header(
        pr: PR,
        review: Review | None,
        findings: int,
        actions: list[PendingAction],
        state: SessionState,
    ) -> Text:
        text = Text()
        text.append(f"#{pr.number}  {pr.title}\n", style="bold")
        text.append(f"by {pr.author}", style="dim")
        text.append(" · assigned to you" if pr.assigned_to_me else "", style="magenta")
        text.append("\n")
        if state.ask:
            text.append("⚠ Waiting for you to allow something", style="bold yellow")
        elif pr.state == PRState.REVIEWING:
            text.append("⟳ Reviewing", style="bold cyan")
            if state.progress:
                text.append(f" · {_short(state.progress, 80)}", style="dim")
        elif review and review.status == ReviewStatus.FAILED:
            text.append(f"✗ Review failed: {review.error}", style="bold red")
        elif review and review.status == ReviewStatus.FINISHED:
            style = "bold yellow" if actions else "bold green"
            text.append("✓ Review done", style=style)
            text.append(f" · {findings} finding{'s' if findings != 1 else ''}")
            if actions:
                text.append(f" · {len(actions)} pending action", style="yellow")
        elif review is None:
            text.append("○ Not reviewed", style="bold")
            text.append(" · press r to review", style="dim")
        return text

    def focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_leave_prompt(self) -> None:
        self.screen.query_one("#prs").focus()

    @on(Input.Submitted, "#prompt")
    async def send(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or not self.pr:
            return
        try:
            await self.purr_app.bus.send_message(self.pr.ref, text)
        except RequestError as e:
            self.notify(str(e), severity="warning")
            return
        event.input.value = ""
        self.append(str(self.pr.ref), {"kind": "user", "text": text})
