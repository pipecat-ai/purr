"""Purr's screens.

One main screen holds both views, the repo list (home) and a repo's PRs, in a
content switcher. Both are built at startup, so moving between them only swaps
which one is visible and refreshes its data: no screen gets composed on the way.
Dialogs are modal screens on top.
"""

import asyncio
import os
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, cast

from rich.table import Table
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
from purr.tui.overview import short_age
from purr.tui.session import NUMBER_STYLE, PRSession, SessionState

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

OTHERS = "others"
_GROUPS = [
    (PRState.NEEDS_YOU, "⚠", "NEEDS YOU", "bold yellow"),
    (PRState.REVIEWING, "●", "REVIEWING", "bold cyan"),
    (PRState.NOT_REVIEWED, "○", "NOT REVIEWED", "bold"),
    (PRState.DONE, "✓", "DONE", "bold green"),
    (OTHERS, "◌", "OTHERS REVIEWING", "bold"),
]
_ICONS = {key: icon for key, icon, *_ in _GROUPS}
NEARBY = 10  # PRs below the cursor whose details are fetched ahead
DIFFS_AHEAD = 3  # of those, whose diffs too (one request each, and bigger)


def group(pr: PR) -> str:
    """Where a PR goes in the list: its review state, or "others" if someone else took it."""
    if pr.state == PRState.NOT_REVIEWED and pr.others_on_it:
        return OTHERS
    return pr.state


def _assignees(pr: PR) -> Text:
    """'you', 'alice', 'you +1' (your login is among ``assignees`` when it's yours)."""
    if pr.assigned_to_me:
        first, rest = Text("you", style="magenta"), max(len(pr.assignees) - 1, 0)
    elif pr.assignees:
        first, rest = Text(pr.assignees[0], style="cyan"), len(pr.assignees) - 1
    else:
        return Text("")
    return first.append(f" +{rest}", style="dim") if rest else first


GAP = 3  # spaces between the number, title, assignees and author


def _row(*cells: Text | str, number_width: int = 5) -> Table:
    """A PR list row: mark, number, title, assignees, author, opened (on the right edge).

    ``number_width`` is the widest PR number's in the list (``#6934`` is 5).
    """
    columns = [
        {"width": 1},
        {"width": number_width},
        {"ratio": 1, "no_wrap": True, "overflow": "ellipsis"},
        {"width": len("ASSIGNED"), "no_wrap": True, "overflow": "ellipsis"},
        {"width": 14, "no_wrap": True, "overflow": "ellipsis"},
        {"width": 6, "justify": "right"},
    ]
    gaps = [1, GAP, GAP, GAP, 1]
    grid = Table.grid(expand=True)
    row: list[Text | str] = []
    for column, cell, gap in zip(columns, cells, [*gaps, 0], strict=True):
        grid.add_column(**column)
        row.append(cell)
        if gap:
            grid.add_column(width=gap)
            row.append("")
    grid.add_row(*row)
    return grid


def matches(pr: PR, search: str) -> bool:
    """Whether every word searched for is in the PR's number, title, author or assignees."""
    text = f"#{pr.number} {pr.title} {pr.author} {' '.join(pr.assignees)}".lower()
    return all(word in text for word in search.lower().split())


def _columns(number_width: int) -> Table:
    headings = ("", "#", "TITLE", "ASSIGNED", "AUTHOR", "OPENED")
    return _row(*(Text(h, style="dim") for h in headings), number_width=number_width)


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
        Binding("enter,o", "open", "Open", key_display="o"),
        Binding("n", "move(1)", "Next", show=False),
        Binding("p", "move(-1)", "Previous", show=False),
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
        self.query_one("#legend").display = bool(alerted)

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

    def action_move(self, delta: int) -> None:
        table = self.query_one(DataTable)
        table.action_cursor_down() if delta > 0 else table.action_cursor_up()

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
        Binding("escape", "back", "Back"),
        Binding("o", "open", "Open"),
        # Two panes, so either way it's the other one (typing included: tab
        # never indents the prompt).
        Binding("tab,shift+tab", "switch_pane", "Switch pane", key_display="tab"),
        Binding("n", "move(1)", "Next"),
        Binding("p", "move(-1)", "Previous"),
        Binding("slash", "search", "Search", key_display="/"),
        Binding("r", "review", "Review"),
        Binding("space", "mark", "Mark"),
        # Whatever the decision box offers: approve or dismiss a proposal,
        # allow or deny a permission request.
        Binding("a", "approve", "Approve"),
        Binding("x", "dismiss", "Dismiss"),
        Binding("d", "diff", "Diff"),
        Binding("z", "zoom", "Zoom"),
        Binding("c", "open_claude", "Claude Code"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.repo: str | None = None
        self._all: list[PR] = []  # the repo's open PRs, as stored
        self._prs: dict[str, PR] = {}  # the ones listed (matching the search), in list order
        self._number_width = 5  # the widest "#6934" in the list
        self._search = ""
        self._selected: dict[str, str] = {}  # repo -> highlighted PR
        self._marked: set[str] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="prs-pane"):
                yield Input(id="search", placeholder="Search: number, title, author…")
                yield OptionList(id="prs")
            yield PRSession(id="session")

    def on_mount(self) -> None:
        self.query_one("#prs-pane").border_title = "PRs"
        self.query_one("#search").display = False

    def focus_main(self) -> None:
        self.query_one(OptionList).focus()

    def _key(self) -> tuple:
        return (self.repo, self.purr_app.data_version)

    async def show(self, repo: str) -> None:
        if repo != self.repo:
            self._set_search("")
        self.repo = repo
        await self.refresh_if_stale()

    async def reload(self) -> None:
        if not self.repo:
            return
        await super().reload()
        self._all = await self.purr_app.purr.store.list_prs(repo=self.repo, open_only=True)
        self._number_width = max((len(f"#{pr.number}") for pr in self._all), default=5)
        self._list()

    def _list(self) -> None:
        """Draw the list: the PRs matching the search, grouped."""
        prs = [pr for pr in self._all if matches(pr, self._search)]
        self._prs = {}
        options: list[Option] = [Option(_columns(self._number_width), disabled=True)] if prs else []
        if self._all and not prs:
            options.append(
                Option(Text(f"No PRs match “{self._search}”.", style="dim"), disabled=True)
            )
        for key, icon, label, style in _GROUPS:
            members = [pr for pr in prs if group(pr) == key]
            if not members:
                continue
            if len(options) > 1:
                options.append(Option(" ", disabled=True))
            heading = Text(f"{icon} {label} ({len(members)})", style=style)
            options.append(Option(heading, disabled=True))
            options += [Option(self._prompt(pr), id=str(pr.ref)) for pr in members]
            self._prs.update((str(pr.ref), pr) for pr in members)
        self._marked &= {str(pr.ref) for pr in self._all}

        option_list = self.query_one(OptionList)
        option_list.set_options(options)
        selected = self._selected.get(self.repo)
        if selected in self._prs:
            option_list.highlighted = option_list.get_option_index(selected)
        else:
            first = next((i for i, o in enumerate(options) if not o.disabled), None)
            option_list.highlighted = first
        self._show_highlighted()

    def _prompt(self, pr: PR) -> Table:
        marked = str(pr.ref) in self._marked
        return _row(
            Text("▪", style="bold magenta") if marked else _ICONS[group(pr)],
            Text(f"#{pr.number}", style=NUMBER_STYLE),
            pr.title,
            _assignees(pr),
            Text(pr.author, style="dim"),
            Text(short_age(pr.created_at), style="dim"),
            number_width=self._number_width,
        )

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
            # A callable, not a coroutine: when moving fast, a run cancelled before
            # it starts must not leave a coroutine behind.
            self.run_worker(partial(self.session.show, pr), exclusive=True, group="session")
            self._load_details(pr)

    @work(exclusive=True, group="details")
    async def _load_details(self, pr: PR) -> None:
        """Fetch the PR's details if needed, then those of the PRs below it."""
        await asyncio.sleep(0.15)  # let the cursor settle when moving fast
        order = list(self._prs)
        index = order.index(str(pr.ref)) if str(pr.ref) in order else 0
        nearby = [self._prs[ref] for ref in order[index + 1 : index + 1 + NEARBY]]
        # The PR on its own (it's the one being waited for) and the rest at once;
        # diffs one by one, the PR's and the next few.
        cache = self.purr_app.details
        batches = [cache.claim(batch) for batch in ([pr], nearby)]
        fetches = [self._fetch_details(batch) for batch in batches if batch]
        fetches += [self._fetch_diff(p) for p in cache.claim_diffs([pr, *nearby[:DIFFS_AHEAD]])]
        if fetches:
            # Shielded: once asked, the answers are worth keeping even if the
            # cursor has moved on.
            await asyncio.shield(asyncio.gather(*fetches))

    async def _fetch_details(self, prs: list[PR]) -> None:
        try:
            fetched, error = await self.purr_app.bus.details([pr.ref for pr in prs]), None
        except Exception as e:  # never let a background fetch take the UI down
            fetched, error = {}, str(e) or type(e).__name__
        for pr in prs:
            state = self.purr_app.sessions.setdefault(str(pr.ref), SessionState())
            changed = self.purr_app.details.put(pr, fetched.get(str(pr.ref)))
            changed = changed or state.details_error != error
            state.details_error = error
            shown = self.session.pr
            if changed and shown and shown.ref == pr.ref:
                await self.session.details_changed()

    async def _fetch_diff(self, pr: PR) -> None:
        try:
            diff, error = await self.purr_app.bus.diff(pr.ref), None
        except Exception as e:  # never let a background fetch take the UI down
            diff, error = None, str(e) or type(e).__name__
        state = self.purr_app.sessions.setdefault(str(pr.ref), SessionState())
        changed = self.purr_app.details.put_diff(pr, diff) or state.diff_error != error
        state.diff_error = error
        shown = self.session.pr
        if changed and shown and shown.ref == pr.ref:
            await self.session.details_changed()

    @property
    def session(self) -> PRSession:
        return self.query_one(PRSession)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "open":  # in the PR's pane, it's already open
            return not self.session.has_focus_within
        if action == "search":  # while typing, / is a slash
            return not self.session.query_one("#prompt").has_focus
        return True

    @on(OptionList.OptionSelected)
    def action_open(self) -> None:
        """Into the selected PR's pane."""
        if self.selected_pr():
            self.session.focus_transcript()

    def action_switch_pane(self) -> None:
        """From the list (or its search) to the PR's pane, and back."""
        option_list = self.query_one(OptionList)
        if self.session.has_focus_within:
            if self.query_one("#prs-pane").display:  # not while zoomed
                option_list.focus()
        elif self.query_one("#search").has_focus:
            option_list.focus()
        else:
            self.action_open()

    # -- Search ---------------------------------------------------------------------

    def _set_search(self, text: str) -> None:
        search = self.query_one("#search", Input)
        self._search = text
        if search.value != text:
            search.value = text
        search.display = bool(text) or search.has_focus

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    @on(Input.Changed, "#search")
    def _searching(self, event: Input.Changed) -> None:
        self._search = event.value
        self._list()

    @on(Input.Submitted, "#search")
    def _searched(self) -> None:
        self.query_one(OptionList).focus()
        self._set_search(self._search.strip())

    async def action_back(self) -> None:
        """Out of the search, then out of the repo."""
        if self._search or self.query_one("#search").has_focus:
            self._set_search("")
            self.query_one(OptionList).focus()
            self._list()
        else:
            await self.purr_app.action_home()

    # -- Keys -----------------------------------------------------------------------

    def action_move(self, delta: int) -> None:
        """The next or previous PR, from either pane."""
        option_list = self.query_one(OptionList)
        option_list.action_cursor_down() if delta > 0 else option_list.action_cursor_up()

    def action_mark(self) -> None:
        pr = self.selected_pr()
        if not pr:
            return
        self._marked ^= {str(pr.ref)}
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        option_list.replace_option_prompt(str(pr.ref), self._prompt(pr))
        if index is not None and index + 1 < option_list.option_count:
            option_list.action_cursor_down()

    @work(group="keys")
    async def action_review(self) -> None:
        targets = [self._prs[ref] for ref in self._marked if ref in self._prs]
        if not targets and (pr := self.selected_pr()):
            targets = [pr]
        taken = [pr for pr in targets if pr.others_on_it]
        if taken and not await self.app.push_screen_wait(
            ConfirmScreen(
                "Someone's already on "
                + ", ".join(f"#{pr.number} ({', '.join(pr.assignees)})" for pr in taken)
                + ". Review anyway?",
                "You'd be added as an assignee too.",
            )
        ):
            return
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
        actions = await self.purr_app.purr.store.pending_actions(pr.ref) if pr else []
        if not actions:
            return
        try:
            url, warnings = await self.purr_app.bus.approve(actions[0].id)
        except RequestError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"Posted: {url}" if url else "Posted")
        for warning in warnings:
            self.notify(warning, severity="warning")

    @work(group="keys")
    async def action_dismiss(self) -> None:
        if state := self._asking():
            await self._answer(state, False)
            return
        pr = self.selected_pr()
        actions = await self.purr_app.purr.store.pending_actions(pr.ref) if pr else []
        if actions:
            try:
                await self.purr_app.bus.dismiss(actions[0].id)
            except RequestError as e:
                self.notify(str(e), severity="warning")

    async def _answer(self, state: SessionState, allow: bool) -> None:
        assert state.ask
        try:
            await self.purr_app.bus.answer(state.ask.request_id, allow)
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

    async def action_diff(self) -> None:
        """The PR's full diff in your pager (gh's pager settings, or less)."""
        pr = self.selected_pr()
        if not pr:
            return
        env = dict(os.environ)
        env.setdefault("PAGER", "less")
        # gh's default LESS=FRX quits at once on a diff that fits the screen,
        # which would hand the terminal straight back to Purr.
        env.setdefault("LESS", "R")
        command = ["gh", "pr", "diff", str(pr.number), "--repo", pr.repo]
        with self.app.suspend():
            process = await asyncio.create_subprocess_exec(*command, env=env)
            await process.wait()

    def action_zoom(self) -> None:
        pane = self.query_one("#prs-pane")
        pane.display = not pane.display
        if pane.display:
            self.query_one(OptionList).focus()
        elif not self.session.has_focus_within:
            self.session.focus_transcript()


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


class InstructionsScreen(ModalScreen):
    """Which instructions to edit: how you like reviews, or how you like code."""

    BINDINGS = [
        Binding("r", "pick('review')", "Review style"),
        Binding("c", "pick('code')", "Code style"),
        Binding("escape", "pick('')", "Cancel", show=False),
    ]

    def __init__(self, repo: str | None):
        super().__init__()
        self._repo = repo

    def compose(self) -> ComposeResult:
        scope = f"for [b]{self._repo}[/b]" if self._repo else "for [b]all repos[/b]"
        with Vertical(classes="dialog"):
            yield Label(f"Your instructions {scope}")
            yield Label("[b]r[/b]  review style: how reviews should go")
            yield Label("[b]c[/b]  code style: what good code looks like to you")
            yield Label("Opens in $EDITOR. Repo files add to the general ones.", classes="hint")

    def action_pick(self, kind: str) -> None:
        from purr.review.instructions import Kind

        self.dismiss(Kind(kind) if kind else None)
