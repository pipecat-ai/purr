"""Purr's screens.

One main screen holds both views, the repo list (home) and a repo's PRs, in a
content switcher. Both are built at startup, so moving between them only swaps
which one is visible and refreshes its data: no screen gets composed on the way.
Dialogs are modal screens on top.
"""

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from purr.messages import REVIEW_WORKER
from purr.models import PR, PRState, RepoSummary
from purr.review.agent import claude_code_command
from purr.tui.bridge import RequestError
from purr.tui.session import PRSession

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_GROUPS = [
    (PRState.NEEDS_YOU, "⚠", "NEEDS YOU", "bold yellow"),
    (PRState.REVIEWING, "●", "REVIEWING", "bold cyan"),
    (PRState.NOT_REVIEWED, "○", "NOT REVIEWED", "bold"),
    (PRState.DONE, "✓", "DONE", "bold green"),
]


def ago(timestamp: str | None) -> str:
    """'just now', '5 min ago', '3 h ago', '2 d ago'."""
    if not timestamp:
        return "-"
    then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    seconds = (datetime.now(UTC) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _count(n: int) -> str:
    return str(n) if n else "-"


class PurrView(Widget):
    """A view in the main screen's switcher."""

    _drawn: tuple | None = None  # what the view last drew, to skip redundant redraws

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def _key(self) -> tuple:
        return (self.purr_app.data_version,)

    async def refresh_if_stale(self) -> None:
        """Redraw only if the data changed since the last draw."""
        if self._drawn != self._key():
            await self.reload()

    async def reload(self) -> None:
        """Redraw from the store."""
        self._drawn = self._key()


class ReposView(PurrView):
    """Home: one row per repo, alphabetical, with what's going on in each."""

    BINDINGS = [
        Binding("enter", "open", "Open"),
        Binding("plus", "add", "Add repo"),
        Binding("minus", "remove", "Remove repo"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._watched: set[str] = set()

    def compose(self) -> ComposeResult:
        yield DataTable(id="repo-table", cursor_type="row")
        yield Static("No repos yet. Press [b]+[/b] to watch one.", id="empty")
        yield Static("[yellow]●[/] something new you haven't looked at", id="legend")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("", key="number", width=2)
        table.add_column("REPO", key="repo")
        table.add_column("NEEDS YOU", key="needs_you")
        table.add_column("REVIEWING", key="reviewing")
        table.add_column("NOT REVIEWED", key="not_reviewed")
        table.add_column("LAST ACTIVITY", key="activity")
        self.set_interval(30, self.reload)  # keeps "last activity" fresh

    def focus_main(self) -> None:
        self.query_one(DataTable).focus()

    async def reload(self) -> None:
        await super().reload()
        summaries = await self.purr_app.purr.store.repo_summaries()
        self.purr_app.repo_names = [s.name for s in summaries]
        self._watched = {s.name for s in summaries if s.watched}
        table = self.query_one(DataTable)
        selected = self.selected_repo()
        alerted = {pr.split("#")[0] for pr in self.purr_app.alerts}
        table.clear()
        for i, summary in enumerate(summaries, start=1):
            table.add_row(*self._cells(i, summary, summary.name in alerted), key=summary.name)
        if selected:
            self.select_repo(selected)
        self.query_one("#empty").display = not self._watched

    @staticmethod
    def _cells(i: int, summary: RepoSummary, alerted: bool) -> list:
        name = Text(summary.name)
        if alerted:
            name.append(" ●", style="bold yellow")
        if not summary.watched:
            name.append("  (assigned to you)", style="dim")
        needs = Text(_count(summary.needs_you), style="bold yellow" if summary.needs_you else "")
        return [
            str(i) if i <= 9 else "",
            name,
            needs,
            _count(summary.reviewing),
            _count(summary.not_reviewed),
            ago(summary.last_activity),
        ]

    def selected_repo(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def select_repo(self, name: str) -> None:
        if name in self.purr_app.repo_names:
            self.query_one(DataTable).move_cursor(row=self.purr_app.repo_names.index(name))

    @on(DataTable.RowSelected)
    async def action_open(self) -> None:
        if repo := self.selected_repo():
            await self.purr_app.open_repo(repo)

    def action_add(self) -> None:
        async def added(name: str | None) -> None:
            if name:
                await self.reload()
                self.select_repo(name)

        self.app.push_screen(AddRepoScreen(), added)

    def action_remove(self) -> None:
        repo = self.selected_repo()
        if not repo:
            return
        if repo not in self._watched:
            self.notify(f"{repo} isn't watched; it's listed because a PR there concerns you.")
            return

        async def confirmed(yes: bool | None) -> None:
            if yes:
                self.remove(repo)

        self.app.push_screen(
            ConfirmScreen(
                f"Stop watching [b]{repo}[/b]?",
                "Its pool goes away. PRs assigned to you there stay.",
            ),
            confirmed,
        )

    @work
    async def remove(self, repo: str) -> None:
        try:
            await self.purr_app.bus.remove_repo(repo)
        except RequestError as e:
            self.notify(str(e), severity="warning")


class RepoView(PurrView):
    """A repo: its PRs grouped by state on the left, the selected PR's session on the right.

    One instance serves every repo; it remembers the selected PR per repo.
    """

    BINDINGS = [
        Binding("escape", "app.home", "Repos"),
        Binding("r", "review", "Review"),
        Binding("space", "mark", "Mark"),
        Binding("a", "approve", "Approve"),
        Binding("x", "dismiss", "Dismiss"),
        Binding("y", "answer(True)", "Allow", show=False),
        Binding("n", "answer(False)", "Deny", show=False),
        Binding("z", "zoom", "Zoom"),
        Binding("o", "open_claude", "Claude Code"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.repo: str | None = None
        self._prs: dict[str, PR] = {}
        self._selected: dict[str, str] = {}  # repo -> highlighted PR
        self._marked: set[str] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield OptionList(id="prs")
            yield PRSession(id="session")

    def focus_main(self) -> None:
        self.query_one(OptionList).focus()

    def _key(self) -> tuple:
        return (self.repo, self.purr_app.data_version)

    async def show(self, repo: str) -> None:
        self.repo = repo
        await self.refresh_if_stale()

    async def reload(self) -> None:
        if not self.repo:
            return
        await super().reload()
        prs = await self.purr_app.purr.store.list_prs(repo=self.repo, open_only=True)
        self._prs = {str(pr.ref): pr for pr in prs}
        self._marked &= set(self._prs)
        options: list[Option] = []
        for state, icon, label, style in _GROUPS:
            group = [pr for pr in prs if pr.state == state]
            if not group:
                continue
            if options:
                options.append(Option(" ", disabled=True))
            heading = Text(f"{icon} {label} ({len(group)})", style=style)
            options.append(Option(heading, disabled=True))
            options += [Option(self._prompt(pr, icon), id=str(pr.ref)) for pr in group]

        option_list = self.query_one(OptionList)
        option_list.set_options(options)
        selected = self._selected.get(self.repo)
        if selected in self._prs:
            option_list.highlighted = option_list.get_option_index(selected)
        else:
            first = next((i for i, o in enumerate(options) if not o.disabled), None)
            option_list.highlighted = first
        self._show_highlighted()

    def _prompt(self, pr: PR, icon: str) -> Text:
        marked = str(pr.ref) in self._marked
        text = Text("▪ " if marked else f"{icon} ", style="bold magenta" if marked else "")
        text.append(f"#{pr.number:<6}", style="bold")
        if pr.assigned_to_me:
            text.append("you ", style="bold magenta")
        text.append(pr.title)
        return text

    def selected_pr(self) -> PR | None:
        option = self.query_one(OptionList).highlighted_option
        return self._prs.get(option.id) if option and option.id else None

    def select(self, pr: str, *, repo: str) -> None:
        """Select a PR, now if its repo is showing, or when it next is."""
        self._selected[repo] = pr
        if repo == self.repo and pr in self._prs:
            option_list = self.query_one(OptionList)
            option_list.highlighted = option_list.get_option_index(pr)

    @on(OptionList.OptionHighlighted)
    def _show_highlighted(self) -> None:
        pr = self.selected_pr()
        if pr and self.repo:
            self._selected[self.repo] = str(pr.ref)
            self.purr_app.seen(str(pr.ref))
            self.run_worker(self.session.show(pr), exclusive=True, group="session")

    @property
    def session(self) -> PRSession:
        return self.query_one(PRSession)

    @on(OptionList.OptionSelected)
    def _ask(self) -> None:
        if self.selected_pr():
            self.session.focus_prompt()

    # -- Keys -----------------------------------------------------------------------

    def action_mark(self) -> None:
        pr = self.selected_pr()
        if not pr:
            return
        self._marked ^= {str(pr.ref)}
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        option_list.replace_option_prompt(str(pr.ref), self._prompt(pr, "○"))
        if index is not None and index + 1 < option_list.option_count:
            option_list.action_cursor_down()

    @work(group="keys")
    async def action_review(self) -> None:
        targets = [self._prs[ref] for ref in self._marked if ref in self._prs]
        if not targets and (pr := self.selected_pr()):
            targets = [pr]
        self._marked.clear()
        for pr in targets:
            try:
                await self.purr_app.bus.start_review(pr.ref)
            except RequestError as e:
                self.notify(str(e), severity="warning")
                continue
            if not pr.assigned_to_me:
                # Taking it from the pool: assign yourself, so teammates see it's taken.
                try:
                    await self.purr_app.bus.assign(pr.ref)
                except RequestError as e:
                    self.notify(str(e), severity="warning")
        if targets:
            self.notify(f"Reviewing {len(targets)} PR{'s' if len(targets) != 1 else ''}")

    @work(group="keys")
    async def action_approve(self) -> None:
        pr = self.selected_pr()
        actions = await self.purr_app.purr.store.pending_actions(pr.ref) if pr else []
        if not actions:
            return
        try:
            url = await self.purr_app.bus.approve(actions[0].id)
        except RequestError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"Posted: {url}" if url else "Posted")

    @work(group="keys")
    async def action_dismiss(self) -> None:
        pr = self.selected_pr()
        actions = await self.purr_app.purr.store.pending_actions(pr.ref) if pr else []
        if actions:
            try:
                await self.purr_app.bus.dismiss(actions[0].id)
            except RequestError as e:
                self.notify(str(e), severity="warning")

    @work(group="keys")
    async def action_answer(self, allow: bool) -> None:
        pr = self.selected_pr()
        ask = self.purr_app.sessions.get(str(pr.ref)) if pr else None
        if ask and ask.ask:
            try:
                await self.purr_app.bus.answer(ask.ask.request_id, allow)
            except RequestError as e:
                self.notify(str(e), severity="warning")

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
        command = claude_code_command(review.session_id, str(pr.ref))
        # Purr keeps running underneath (polls, other reviews) while Claude Code
        # has the terminal.
        with self.app.suspend():
            process = await asyncio.create_subprocess_exec(*command, cwd=review.worktree_path)
            await process.wait()
        # The session may have grown in Claude Code: reload its transcript.
        self.purr_app.sessions.pop(str(pr.ref), None)
        self.session.pr = None
        await self.session.show(pr)

    def action_zoom(self) -> None:
        option_list = self.query_one(OptionList)
        option_list.display = not option_list.display
        if not option_list.display:
            self.session.focus_prompt()


class MainScreen(Screen):
    """Top bar, the current view (repo list or a repo), and the footer."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("Purr", id="crumb")
            yield Static(id="alert")
        with ContentSwitcher(initial="repos", id="views"):
            yield ReposView(id="repos")
            yield RepoView(id="repo")
        yield Footer()

    @property
    def repos(self) -> ReposView:
        return self.query_one(ReposView)

    @property
    def repo(self) -> RepoView:
        return self.query_one(RepoView)

    @property
    def current(self) -> PurrView:
        return self.repos if self.query_one(ContentSwitcher).current == "repos" else self.repo

    def set_alert(self, text: str) -> None:
        if self.is_mounted:  # events can arrive before the screen is up
            self.query_one("#alert", Static).update(text)

    async def show_home(self) -> None:
        self.query_one("#crumb", Static).update("Purr")
        self.query_one(ContentSwitcher).current = "repos"
        self.repos.focus_main()
        await self.repos.refresh_if_stale()

    async def show_repo(self, name: str) -> None:
        app = cast("PurrApp", self.app)
        number = f"{app.repo_names.index(name) + 1} " if name in app.repo_names[:9] else ""
        self.query_one("#crumb", Static).update(f"Purr › {number}{name}")
        self.query_one(ContentSwitcher).current = "repo"
        self.repo.focus_main()
        await self.repo.show(name)


class AddRepoScreen(ModalScreen[str | None]):
    """Ask for a repo to watch and check it on GitHub."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("[b]Watch a repo[/b]")
            yield Input(placeholder="owner/name or GitHub URL")
            yield Static(id="error")
            yield Label("enter add · esc cancel", classes="hint")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.query_one("#error", Static).update(Text("Checking GitHub…", style="dim"))
            self.add(event.value)

    @work(exclusive=True)
    async def add(self, value: str) -> None:
        try:
            name = await cast("PurrApp", self.app).bus.add_repo(value)
        except RequestError as e:
            self.query_one("#error", Static).update(Text(str(e), style="red"))
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, question: str, detail: str = ""):
        super().__init__()
        self._question = question
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._question)
            if self._detail:
                yield Label(self._detail, classes="hint")
            yield Label("y yes · n no", classes="hint")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)
