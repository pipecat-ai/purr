"""Home: the repos, with what's waiting in each."""

from datetime import UTC, datetime

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from purr.jobs import PurrError
from purr.messages import UnwatchRepo
from purr.models import RepoSummary
from purr.tui.dialogs import AddRepoScreen, ConfirmScreen
from purr.tui.keyhelp import KEYS
from purr.tui.lists import GROUP_GAP, GroupedList, blank, separated, titled_line
from purr.tui.views import PurrView

_REPO_COLUMNS = ("", "REPO", "NEEDS YOU", "REVIEWING", "NOT REVIEWED", "LAST ACTIVITY")


def _repo_row(*cells: Text | str, header: bool = False) -> Table:
    """A row of the repo list: number, name, then counts and when, in columns."""
    widths = [2, None, 10, 10, 13, 14]
    grid = Table.grid(expand=True, padding=(0, 1))
    for width in widths:
        if width is None:
            grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        else:
            grid.add_column(width=width, no_wrap=True)
    grid.add_row(*(Text(str(c), style="dim") if header else c for c in cells))
    return grid


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


class ReposView(PurrView):
    """Home: the repos you watch, then those listed only because PRs there are yours.

    Watching a repo (``+``) shows all its open PRs; the others show only the PRs
    assigned to you, which you can review all the same. Alphabetical within
    each group; ``1``-``9`` follow the order on screen.
    """

    BINDINGS = [
        KEYS,
        Binding("o", "open", "Open"),
        Binding("n", "move(1)", "Next", show=False),
        Binding("p", "move(-1)", "Previous", show=False),
        Binding("plus", "add", "Watch repo"),
        Binding("minus", "remove", "Stop watching"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._watched: set[str] = set()

    def compose(self) -> ComposeResult:
        yield GroupedList(id="repo-list")
        yield Static(id="empty")
        yield Static("[yellow]●[/] something new you haven't looked at", id="legend")

    def on_mount(self) -> None:
        self.set_interval(30, self.reload)  # keeps "last activity" fresh

    def focus_main(self) -> None:
        self.query_one(OptionList).focus()

    async def reload(self) -> None:
        await super().reload()
        summaries = await self.purr_app.purr.store.repo_summaries()
        watched = [s for s in summaries if s.watched]
        yours = [s for s in summaries if not s.watched]
        self.purr_app.repo_names = [s.name for s in watched + yours]
        self._watched = {s.name for s in watched}
        alerted = {pr.split("#")[0] for pr in self.purr_app.alerts}
        selected = self.selected_repo()
        options = self._options(watched, yours, alerted)
        repo_list = self.query_one(OptionList)
        repo_list.set_options(options if summaries else [])
        repo_list.display = bool(summaries)
        if selected in self.purr_app.repo_names:
            self.select_repo(selected)
        elif summaries:
            repo_list.highlighted = next(i for i, o in enumerate(options) if not o.disabled)
        # Hidden while there were no repos, it couldn't keep focus: give it back.
        if summaries and self.purr_app.location is None and self.app.focused is None:
            repo_list.focus()
        empty = self.query_one("#empty", Static)
        empty.display = not summaries
        empty.set_class(not summaries, "alone")
        empty.update("No repos yet. Press [b]+[/b] to watch one and see its open PRs.")
        self.query_one("#legend").display = bool(alerted)

    def _options(
        self, watched: list[RepoSummary], yours: list[RepoSummary], alerted: set[str]
    ) -> list[Option]:
        """The column headings, then the watched repos and the others, each group headed."""
        options = [Option(_repo_row(*_REPO_COLUMNS, header=True), disabled=True)]
        number = 0
        for summaries, heading, note, style in [
            (watched, "● WATCHED", "all their open PRs", "bold cyan"),
            (yours, "○ NOT WATCHED", "only the PRs assigned to you · + watches one", "bold"),
        ]:
            if not summaries:
                continue
            if len(options) > 1:
                options += blank(GROUP_GAP)
            title = Text(f"{heading} ({len(summaries)})", style=style)
            title.append(f"  {note}", style="dim")
            options.append(Option(titled_line(title, style), disabled=True))
            rows = []
            for summary in summaries:
                number += 1
                cells = self._cells(number, summary, summary.name in alerted)
                rows.append(Option(_repo_row(*cells), id=summary.name))
            options += separated(rows)
        return options

    @staticmethod
    def _cells(i: int, summary: RepoSummary, alerted: bool) -> list:
        name = Text(summary.name)
        if alerted:
            name.append(" ●", style="bold yellow")
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
        option = self.query_one(OptionList).highlighted_option
        return option.id if option and option.id else None

    def select_repo(self, name: str) -> None:
        repo_list = self.query_one(OptionList)
        if name in self.purr_app.repo_names:
            repo_list.highlighted = repo_list.get_option_index(name)

    @on(OptionList.OptionSelected, "#repo-list")
    async def action_open(self) -> None:
        if repo := self.selected_repo():
            await self.purr_app.open_repo(repo)

    def action_move(self, delta: int) -> None:
        repo_list = self.query_one(OptionList)
        repo_list.action_cursor_down() if delta > 0 else repo_list.action_cursor_up()

    def action_add(self) -> None:
        async def added(name: str | None) -> None:
            if name:
                await self.reload()
                self.select_repo(name)

        # On a repo that's only listed for your PRs: that one, ready to watch.
        selected = self.selected_repo()
        suggested = selected if selected and selected not in self._watched else ""
        self.app.push_screen(AddRepoScreen(suggested), added)

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
            await self.purr_app.bus.request(UnwatchRepo(repo=repo))
        except PurrError as e:
            self.notify(str(e), severity="warning")
