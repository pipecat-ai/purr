"""Purr's screens.

One main screen holds both views, the repo list (home) and a repo's PRs, in a
content switcher. Both are built at startup, so moving between them only swaps
which one is visible and refreshes its data: no screen gets composed on the way.
Dialogs are modal screens on top.
"""

import asyncio
import os
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal, cast

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual._widget_navigation import find_next_enabled_no_wrap
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from purr.claude import NO_CLAUDE_CODE, claude_cli
from purr.jobs import PurrError
from purr.messages import (
    REVIEW_WORKER,
    AnswerPermission,
    ApproveAction,
    AssignMe,
    CheckMerge,
    DismissAction,
    DismissReplies,
    FetchDetails,
    GetDiff,
    GetWorktree,
    MergePR,
    MergeReadiness,
    PlanReviews,
    PostComments,
    UnwatchRepo,
    WatchRepo,
)
from purr.models import PR, CheckState, PRRef, PRState, RepoSummary
from purr.review.agent import claude_code_command
from purr.review.skills import Skill
from purr.tui.diffview import DiffPane
from purr.tui.keys import REPO_KEYS, pane_keys
from purr.tui.overview import short_age
from purr.tui.session import NUMBER_STYLE, PRSession, SessionState, outcome_mark
from purr.tui.working import COLOR, FRAME_SECONDS, FRAMES

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

OTHERS = "others"
REQUESTED = "requested"
# Done comes before the pool of unreviewed PRs (hundreds, in a big repo), so
# the ones you finished lately are at hand: the latest few, search finds the rest.
# Before both, what's under way: PRs asking for your review (someone's waiting on
# you), then those others are reviewing.
_GROUPS = [
    (PRState.NEEDS_YOU, "⚠", "NEEDS YOU", "bold yellow"),
    (PRState.REVIEWING, "●", "REVIEWING", "bold cyan"),
    (REQUESTED, "◇", "REVIEW REQUESTED", "bold magenta"),
    (OTHERS, "◌", "OTHERS REVIEWING", "bold"),
    (PRState.DONE, "✓", "DONE", "bold green"),
    (PRState.NOT_REVIEWED, "○", "NOT REVIEWED", "bold"),
]
DONE_SHOWN = 5
_ICONS = {key: icon for key, icon, *_ in _GROUPS}
NEARBY = 10  # PRs below the cursor whose details are fetched ahead


class GroupedList(OptionList):
    """A list of things in groups under headings (PRs, repos), the headings disabled.

    It stops at either end rather than wrapping round. Scrolling to the first thing
    in a group brings its heading along, which can't be highlighted to come into view
    itself (and, above the first group, the column headings).
    """

    def action_cursor_up(self) -> None:
        self._step(-1)

    def action_cursor_down(self) -> None:
        self._step(1)

    def _step(self, direction: Literal[-1, 1]) -> None:
        found = find_next_enabled_no_wrap(self.options, self.highlighted, direction)
        if found is not None:
            self.highlighted = found

    def scroll_to_highlight(self, top: bool = False) -> None:
        super().scroll_to_highlight(top)
        lines = getattr(self, "_index_to_line", {})  # Textual's insides: none, no harm
        line = lines.get(self._headed(self.highlighted))
        if line is not None and line < self.scroll_offset.y:
            self.scroll_to(y=line, animate=False, immediate=True)

    def _headed(self, index: int | None) -> int | None:
        """The first of the headings right above ``index`` (itself, if there are none)."""
        while index and self.get_option_at_index(index - 1).disabled:
            index -= 1
        return index


class PRList(GroupedList):
    """The PR list, which can redraw one row without rebuilding the rest.

    ``replace_option_prompt`` lays out every row again: fine once, but a spinning
    mark redraws several times a second (300 PRs: most of a CPU).
    """

    BINDINGS = pane_keys(
        ["open", "search", "review", "mark", "approve", "dismiss", "merge", "diff", "zoom"]
        + ["shell", "open_claude", "switch_pane"],
        switch_pane="PR",
    )

    async def action_repo(self, action: str) -> None:
        await cast("PurrApp", self.app).main.repo.run_action(action)

    def redraw_option(self, option_id: str, prompt: Table) -> None:
        """A new look for a row of the same height."""
        try:
            option = self.get_option(option_id)
            option._set_prompt(prompt)
            cache = self._option_render_cache
            for key in [key for key in cache.keys() if key[0] is option]:  # noqa: SIM118 (no __iter__)
                cache.discard(key)
            line = self._line_cache.index_to_line.get(self.get_option_index(option_id))
        except AttributeError:  # Textual's insides moved: the slow way, still right
            self.replace_option_prompt(option_id, prompt)
            return
        self.refresh_line(line) if line is not None else self.refresh()


def group(pr: PR) -> str:
    """Where a PR goes in the list: its review state; or, not reviewed, "requested" if it
    asks for your review, "others" if someone else took it."""
    if pr.state == PRState.NOT_REVIEWED and pr.requested_from_me and not pr.assigned_to_me:
        return REQUESTED
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


def _review_anyway(taken: list[PR]) -> "ConfirmScreen":
    """Ask before reviewing PRs someone else is already on."""
    who = ", ".join(f"#{pr.number} ({', '.join(pr.assignees)})" for pr in taken)
    return ConfirmScreen(
        f"Someone's already on {who}. Review anyway?", "You'd be added as an assignee too."
    )


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
    """Home: the repos you watch, then those listed only because PRs there are yours.

    Watching a repo (``+``) shows all its open PRs; the others show only the PRs
    assigned to you, which you can review all the same. Alphabetical within
    each group; ``1``-``9`` follow the order on screen.
    """

    BINDINGS = [
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
        for summaries, heading, note in [
            (watched, "● WATCHED", "all their open PRs"),
            (yours, "○ NOT WATCHED", "only the PRs assigned to you · + watches one"),
        ]:
            if not summaries:
                continue
            if len(options) > 1:
                options.append(Option(" ", disabled=True))
            title = Text(f"{heading} ({len(summaries)})", style="bold")
            title.append(f"  {note}", style="dim")
            options.append(Option(title, disabled=True))
            for summary in summaries:
                number += 1
                cells = self._cells(number, summary, summary.name in alerted)
                options.append(Option(_repo_row(*cells), id=summary.name))
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


def _shell() -> str:
    """Your shell: $SHELL, or Windows' (%COMSPEC%), or sh."""
    if shell := os.environ.get("SHELL"):
        return shell
    if os.name == "nt":
        return os.environ.get("COMSPEC") or "cmd.exe"
    return "/bin/sh"


MERGE_RECHECK = 10.0  # seconds between asking GitHub again while checks run


def _checks_running(merge: MergeReadiness | None) -> bool:
    return bool(merge) and any(c.state == CheckState.RUNNING for c in merge.check.checks)


class RepoView(PurrView):
    """A repo: its PRs grouped by state on the left, the selected PR's session on the right.

    One instance serves every repo; it remembers the selected PR per repo.
    """

    # Its keys work from either pane; each pane's footer shows its own (pane_keys).
    BINDINGS = [
        Binding("escape", "back", "Home"),
        *[replace(binding, show=False) for binding in REPO_KEYS.values()],
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.repo: str | None = None
        self._all: list[PR] = []  # the repo's open PRs, as stored
        self._prs: dict[str, PR] = {}  # the ones listed (matching the search), in list order
        self._number_width = 5  # the widest "#6934" in the list
        self._search = ""
        self._searches: dict[str, str] = {}  # repo -> its search, kept until you clear it
        self._selected: dict[str, str] = {}  # repo -> highlighted PR
        self._marked: set[str] = set()
        self._outcomes: dict = {}  # PRRef -> what its latest review found

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="prs-pane"):
                yield Input(id="search", placeholder="Search: number, title, author…")
                yield PRList(id="prs")
            yield DiffPane(id="diff-pane")  # in the list's place, while it's shown (d)
            yield PRSession(id="session")

    def on_mount(self) -> None:
        self.query_one("#prs-pane").border_title = "PRs"
        self.query_one("#search").display = False
        self.query_one(DiffPane).display = False
        self._left = "list"  # what's on the left: the PR list, or a PR's diff
        # PRs whose agent is at work: their mark spins, like the PR pane's line.
        self._spinning: set[str] = set()
        self._frame = 0
        self._spinner = self.set_interval(FRAME_SECONDS, self._spin, pause=True)

    def focus_main(self) -> None:
        self.query_one(OptionList).focus()

    def _key(self) -> tuple:
        return (self.repo, self.purr_app.data_version)

    async def show(self, repo: str) -> None:
        if repo != self.repo:
            self.repo = repo
            self._set_search(self._searches.get(repo, ""))
            self.session.clear()  # not the last repo's PR while this one's loads
        await self.refresh_if_stale()

    async def reload(self) -> None:
        if not self.repo:
            return
        await super().reload()
        store = self.purr_app.purr.store
        self._all = await store.list_prs(repo=self.repo, open_only=True)
        self._outcomes = await store.outcomes(self.repo)
        self._number_width = max((len(f"#{pr.number}") for pr in self._all), default=5)
        self._list()

    def _list(self) -> None:
        """Draw the list: the PRs matching the search, grouped."""
        prs = [pr for pr in self._all if matches(pr, self._search)]
        groups = self._groups(prs)
        self._prs = {str(pr.ref): pr for _, members in groups for pr in members}
        self._spinning = {str(pr.ref) for pr in prs if self._working(str(pr.ref))}
        self._marked &= {str(pr.ref) for pr in self._all}
        self._spinner.resume() if self._spinning else self._spinner.pause()
        if prs:
            options = self._options(groups)
        elif self._all:
            options = [Option(Text(f"No PRs match “{self._search}”.", style="dim"), disabled=True)]
        else:
            options = []

        option_list = self.query_one(OptionList)
        option_list.set_options(options)
        selected = self._selected.get(self.repo)
        if selected in self._prs:
            option_list.highlighted = option_list.get_option_index(selected)
        else:
            first = next((i for i, o in enumerate(options) if not o.disabled), None)
            option_list.highlighted = first
        self._show_highlighted()

    def _groups(self, prs: list[PR]) -> list[tuple[Text, list[PR]]]:
        """The PRs in their groups, each with its heading; empty groups left out."""
        groups = []
        for key, icon, label, style in _GROUPS:
            members = [pr for pr in prs if group(pr) == key]
            if not members:
                continue
            heading = Text(f"{icon} {label} ({len(members)})", style=style)
            if key == PRState.DONE:
                members, note = self._latest_done(members)
                heading.append(note, style="dim")
            groups.append((heading, members))
        return groups

    def _latest_done(self, done: list[PR]) -> tuple[list[PR], str]:
        """Done PRs, latest first: only the latest few, unless searching, and a note saying so."""
        done = sorted(done, key=lambda pr: pr.state_at or "", reverse=True)
        if len(done) <= DONE_SHOWN or self._search:
            return done, ""
        # An older one you went to (an alert, ctrl+p) shows too.
        chosen = self._selected.get(self.repo)
        latest = [pr for i, pr in enumerate(done) if i < DONE_SHOWN or str(pr.ref) == chosen]
        return latest, f"  the latest {DONE_SHOWN}; / finds the others"

    def _options(self, groups: list[tuple[Text, list[PR]]]) -> list[Option]:
        """The column headings, then each group's heading and its PRs, a line apart."""
        options = [Option(_columns(self._number_width), disabled=True)]
        for heading, members in groups:
            if len(options) > 1:
                options.append(Option(" ", disabled=True))
            options.append(Option(heading, disabled=True))
            options += [Option(self._prompt(pr), id=str(pr.ref)) for pr in members]
        return options

    def _working(self, key: str) -> bool:
        state = self.purr_app.sessions.get(key)
        return bool(state and state.working)

    def working_changed(self) -> None:
        """Some review started or stopped working: start or stop its spinning mark."""
        if not self.repo:
            return
        spinning = {key for key in self._prs if self._working(key)}
        changed, self._spinning = spinning ^ self._spinning, spinning
        prs = self.query_one(PRList)
        for key in changed & self._prs.keys():
            prs.redraw_option(key, self._prompt(self._prs[key]))
        self._spinner.resume() if spinning else self._spinner.pause()

    def _spin(self) -> None:
        self._frame += 1
        prs = self.query_one(PRList)
        for key in self._spinning & self._prs.keys():
            prs.redraw_option(key, self._prompt(self._prs[key]))

    def _prompt(self, pr: PR) -> Table:
        marked = str(pr.ref) in self._marked
        outcome = self._outcomes.get(pr.ref) if pr.state != PRState.REVIEWING else None
        if marked:
            mark = Text("▪", style="bold magenta")
        elif str(pr.ref) in self._spinning:
            mark = Text(FRAMES[self._frame % len(FRAMES)], style=f"bold {COLOR}")
        elif outcome:
            mark = outcome_mark(outcome)
        else:
            mark = Text(_ICONS[group(pr)])
        return _row(
            mark,
            Text(f"#{pr.number}", style=NUMBER_STYLE),
            # Something happened since you last looked: like unread mail.
            Text(pr.title, style="bold" if str(pr.ref) in self.purr_app.alerts else ""),
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
        if repo != self.repo:
            return
        if pr in self._prs:
            option_list = self.query_one(OptionList)
            option_list.highlighted = option_list.get_option_index(pr)
        elif any(str(listed.ref) == pr for listed in self._all):
            self._list()  # an older done one: listed now that it's selected

    @on(OptionList.OptionHighlighted)
    def _show_highlighted(self) -> None:
        pr = self.selected_pr()
        if pr and self.diff_shown and self.diff.pr and pr.ref != self.diff.pr.ref:
            self.close_diff()  # another PR (an alert, ctrl+p): the list, to see where
        if pr and self.repo:
            self._leave_merge(self._selected.get(self.repo), str(pr.ref))
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
        # its diff too, so d opens at once.
        cache = self.purr_app.details
        batches = [cache.claim(batch) for batch in ([pr], nearby)]
        fetches = [self._fetch_details(batch) for batch in batches if batch]
        fetches += [self._fetch_diff(p) for p in cache.claim_diffs([pr])]
        if fetches:
            # Shielded: once asked, the answers are worth keeping even if the
            # cursor has moved on.
            await asyncio.shield(asyncio.gather(*fetches))

    async def _fetch_details(self, prs: list[PR]) -> None:
        try:
            request = FetchDetails(prs=[pr.ref for pr in prs])
            fetched, error = (await self.purr_app.bus.request(request)).details, None
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

    async def _fetch_diff(self, pr: PR) -> str | None:
        """Fetch the PR's diff into the cache; why not, if it couldn't be."""
        try:
            diff = (await self.purr_app.bus.request(GetDiff(pr=pr.ref))).diff
        except Exception as e:  # never let a background fetch take the UI down
            self.purr_app.details.put_diff(pr, None)
            return str(e) or type(e).__name__
        self.purr_app.details.put_diff(pr, diff)
        return None

    @property
    def session(self) -> PRSession:
        return self.query_one(PRSession)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "open":  # in the PR's pane, it's already open
            return not self.session.has_focus_within
        if action in ("search", "move", "mark") and self.diff_shown:  # the list's away
            return False
        if action == "search":  # while typing, / is a slash
            return not self.session.query_one("#prompt").has_focus
        return True

    def action_open(self) -> None:
        """Into the selected PR's pane, to read it (o)."""
        if self.selected_pr():
            self.session.focus_transcript()

    @on(OptionList.OptionSelected)
    def _ask(self) -> None:
        """enter on a PR: straight to its prompt, to ask the agent about it."""
        if self.selected_pr():
            self.session.focus_prompt()

    def action_switch_pane(self) -> None:
        """From the list (or its search, or the diff) to the PR's pane, and back."""
        left = self.diff if self.diff_shown else self.query_one("#prs-pane")
        if not self.session.has_focus_within:
            self.action_open()
        elif left.display:  # not while the PR's pane is zoomed
            self._focus_left()

    # -- Search ---------------------------------------------------------------------

    def _set_search(self, text: str) -> None:
        search = self.query_one("#search", Input)
        self._search = text
        if self.repo:
            self._searches[self.repo] = text
        if search.value != text:
            search.value = text
        search.display = bool(text) or search.has_focus

    def _leave_search(self) -> None:
        """Back to the list. The search stays, shown above it, until you clear it."""
        self._set_search(self._search.strip())
        self.query_one("#search").display = bool(self._search)
        self.query_one(OptionList).focus()

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    @on(Input.Changed, "#search")
    def _searching(self, event: Input.Changed) -> None:
        self._search = event.value
        if self.repo:
            self._searches[self.repo] = event.value
        self._list()

    @on(Input.Submitted, "#search")
    def _searched(self) -> None:
        self._leave_search()

    async def action_back(self) -> None:
        """Home, from the list or the PR's pane (tab goes between them).

        From the search box, back to the list (the search stays, shown above it); from
        the prompt, back to the PR's pane (see PRSession): esc again goes home. Nothing's
        lost: the search, and anything half-typed in the prompt, are there when you're back.
        """
        if self.diff_shown:
            self.close_diff()  # back to the list; esc again goes home
            return
        if self.query_one("#search").has_focus:
            self._leave_search()  # back to the list, the search kept; esc again goes home
            return
        if not self.query_one("#prs-pane").display:
            self.action_zoom()  # both panes again, for next time
        self.query_one(PRList).focus()
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
        """Review the marked PRs, or else the selected one."""
        targets = [self._prs[ref] for ref in self._marked if ref in self._prs]
        if not targets and (pr := self.selected_pr()):
            targets = [pr]
        taken = [pr for pr in targets if pr.others_on_it]
        if taken and not await self.app.push_screen_wait(_review_anyway(taken)):
            return
        self._marked.clear()
        if not targets:
            return
        await self._take(targets)
        plural = "s" if len(targets) != 1 else ""
        self.notify(f"Planning {len(targets)} review{plural}")
        # The lead plans them together (a few seconds), then they start.
        try:
            request = PlanReviews(prs=[pr.ref for pr in targets])
            errors = (await self.purr_app.bus.request(request)).errors
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        for error in errors.values():
            self.notify(error, severity="warning")

    async def _take(self, prs: list[PR]) -> None:
        """Taking PRs from the pool: assign yourself, so teammates see they're taken."""
        for pr in prs:
            if pr.assigned_to_me:
                continue
            try:
                await self.purr_app.bus.request(AssignMe(pr=pr.ref))
            except PurrError as e:
                self.notify(str(e), severity="warning")

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
        if not pr or not (job := await self._to_post(pr)):
            return
        try:
            posted = await self.purr_app.bus.request(job)
        except PurrError as e:
            self.notify(str(e), severity="error")
            return
        self.notify(f"Posted: {posted.url}" if posted.url else "Posted")
        for warning in posted.warnings:
            self.notify(warning, severity="warning")

    async def _to_post(self, pr: PR) -> ApproveAction | PostComments | None:
        """What a: the agent's proposal (with your comments), or your comments on their own."""
        store = self.purr_app.purr.store
        if actions := await store.pending_actions(pr.ref):
            return ApproveAction(action_id=actions[0].id)
        return PostComments(pr=pr.ref) if await store.draft_comments(pr.ref) else None

    @work(group="keys")
    async def action_dismiss(self) -> None:
        if state := self._asking():
            await self._answer(state, False)
            return
        pr = self.selected_pr()
        if pr is None:
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

    def _leave_merge(self, left: str | None, shown: str) -> None:
        """Moving on from a PR drops where it stood for merging: m asks again next time."""
        if left and left != shown and (state := self.purr_app.sessions.get(left)):
            state.merge = None

    @work(group="keys")
    async def action_merge(self) -> None:
        """m: where the PR stands for merging; m again, with nothing blocking it: merge."""
        pr = self.selected_pr()
        if pr is None:
            return
        state = self.session.state(pr)
        try:
            if state.merge is None or state.merge.blockers:
                state.merge = await self.purr_app.bus.request(CheckMerge(pr=pr.ref))
            else:
                await self._merge(pr, state)
        except PurrError as e:
            state.merge = None
            self.notify(str(e), severity="warning")
        await self.session.refresh_status()
        self._follow_checks(pr, state)

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

    # -- The diff, in the list's place --------------------------------------------------

    @property
    def diff(self) -> DiffPane:
        return self.query_one(DiffPane)

    @property
    def diff_shown(self) -> bool:
        """The diff's in the list's place (zoomed away, maybe)."""
        return self._left == "diff"

    def _focus_left(self) -> None:
        self.diff.view.focus() if self.diff_shown else self.query_one(PRList).focus()

    async def action_diff(self) -> None:
        """The PR's diff in the list's place, to read line by line, ask about and comment
        on; d again (or esc) closes it. D: the whole diff in your pager."""
        if self.diff_shown:
            self.close_diff()
            return
        pr = self.selected_pr()
        if not pr:
            return
        error = await self._fetch_diff(pr) if self.purr_app.details.diff(pr) is None else None
        if (diff := self.purr_app.details.diff(pr)) is None:
            why = f" ({error})" if error else ""
            message = f"Couldn't get the diff{why}. D opens it in your pager."
            self.notify(message, severity="warning")
            return
        await self.diff.show(pr, diff)
        self._left = "diff"
        self.query_one("#prs-pane").display = False
        self.diff.display = self.session.display = True
        self.diff.view.focus()

    def close_diff(self) -> None:
        """The list again, in the diff's place (and the PR's pane, if the diff was zoomed)."""
        self._left = "list"
        self.diff.display = False
        self.session.display = True
        self.query_one("#prs-pane").display = True
        self.query_one(PRList).focus()

    def diff_changed(self, pr: str) -> None:
        """What's been said on a PR changed: its diff shows it, if it's the one shown."""
        if self.diff_shown and self.diff.pr and str(self.diff.pr.ref) == pr:
            self.run_worker(self.diff.reload(), group="diff-comments")

    @on(DiffPane.Closed)
    def _diff_closed(self) -> None:
        self.close_diff()

    @on(DiffPane.Asked)
    def _asked(self, event: DiffPane.Asked) -> None:
        """You asked about lines of the diff: to the PR's prompt, with the lines."""
        self.session.display = True  # if the diff was zoomed
        self.session.ask_about(event.quote)

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

    def action_thinking(self) -> None:
        """The agents' reasoning in full, or a line per thought."""
        app = self.purr_app
        app.full_thinking = not app.full_thinking
        if self.session.pr is not None:
            self.session.redraw(keep_place=True)
        what = "in full" if app.full_thinking else "a line per thought"
        self.notify(f"The agent's reasoning: {what}", timeout=2)

    def action_zoom(self) -> None:
        """Full screen: the diff, from it, or else the PR's pane; and back."""
        if self.diff_shown and not self.session.has_focus_within:
            self.session.display = not self.session.display
            return
        left = self.diff if self.diff_shown else self.query_one("#prs-pane")
        left.display = not left.display
        if left.display:
            self._focus_left()
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
        # ctrl+p is Purr's finder (the README says), not the "palette" Textual would show.
        yield Footer(show_command_palette=False)

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
        await self.repo.show(name)  # before it shows: never the last repo's list
        self.query_one(ContentSwitcher).current = "repo"
        self.repo.focus_main()


class AddRepoScreen(ModalScreen[str | None]):
    """Ask for a repo to watch and check it on GitHub."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, repo: str = ""):
        super().__init__()
        self._repo = repo  # filled in, ready to watch

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("[b]Watch a repo[/b]")
            yield Input(value=self._repo, placeholder="owner/name or GitHub URL")
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
            name = (await cast("PurrApp", self.app).bus.request(WatchRepo(repo=value))).repo
        except PurrError as e:
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


class NoticeScreen(ModalScreen[None]):
    """Something you should know (it went wrong), until you close it."""

    BINDINGS = [Binding("escape,enter", "close", "Close")]

    def __init__(self, message: str, detail: str = ""):
        super().__init__()
        self._message = message
        self._detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._message)
            if self._detail:
                yield Label(self._detail, classes="hint")
            yield Label("enter or esc closes", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class AlwaysScreen(ModalScreen[str | None]):
    """Where to always allow a rule: this repo (``repo``), everywhere (``all``), or not (None)."""

    BINDINGS = [
        Binding("r", "pick('repo')", "This repo"),
        Binding("e", "pick('all')", "Everywhere"),
        Binding("escape", "pick('')", "Cancel", show=False),
    ]

    def __init__(self, rule: str, repo: str):
        super().__init__()
        self._rule, self._repo = rule, repo

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Always allow")
            yield Label(Text(self._rule, style="bold yellow"))
            yield Label(Text.from_markup(f"[b]r[/b]  in {self._repo}'s reviews"))
            yield Label(
                Text.from_markup("[b]e[/b]  everywhere: your Claude Code settings, Claude Code too")
            )
            yield Label("It's Claude Code's own rule. Change them with i, then p.", classes="hint")

    def action_pick(self, scope: str) -> None:
        self.dismiss(scope or None)


class InstructionsScreen(ModalScreen):
    """Which instructions to edit: how you like reviews, or how you like code.

    Also your standing permissions (``permissions``), Purr's settings
    (``settings``), and, inside a repo, which of its skills its reviews follow
    (``skill``).
    """

    BINDINGS = [
        Binding("r", "pick('review')", "Review style"),
        Binding("c", "pick('code')", "Code style"),
        Binding("p", "pick('permissions')", "Permissions"),
        Binding("s", "pick('skill')", "Review skill"),
        Binding("o", "pick('settings')", "Settings"),
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
            yield Label("[b]p[/b]  permissions: what agents may do without asking")
            if self._repo:
                yield Label("[b]s[/b]  review skill: which of the repo's skills reviews follow")
            yield Label("[b]o[/b]  Purr's settings: models, polling, signature… (all repos)")
            yield Label("Opens in $EDITOR. Repo files add to the general ones.", classes="hint")

    def check_action(self, action: str, parameters: tuple) -> bool:
        return self._repo is not None or parameters != ("skill",)

    def action_pick(self, kind: str) -> None:
        from purr.review.instructions import Kind

        self.dismiss(
            kind if kind in ("skill", "permissions", "settings") else Kind(kind) if kind else None
        )


class ReviewSkillScreen(ModalScreen[str | None]):
    """Which of a repo's skills its reviews follow, if any.

    Dismisses with the skill, ``""`` for Purr's own review, or None (escape):
    leave it as it is, and when asking, ask again next time.

    Args:
        repo: The repo.
        skills: Its skills that may be for reviewing.
        chosen: What its reviews follow now ('' for Purr's own, None: not decided).
        asking: Purr is asking (reviews are waiting), rather than you changing it.
    """

    BINDINGS = [Binding("escape", "cancel", "Not now")]

    def __init__(
        self, repo: str, skills: list[Skill], chosen: str | None = None, *, asking: bool = False
    ):
        super().__init__()
        self._repo = repo
        self._choices = [skill.name for skill in skills] + [""]
        self._skills = skills
        self._chosen = chosen
        self._asking = asking

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="review-skill"):
            if self._asking:
                yield Label(
                    f"[b]{self._repo}[/b] has skills that may be how it reviews PRs. "
                    "Should Purr's reviews there follow one?"
                )
            else:
                yield Label(f"What Purr's reviews in [b]{self._repo}[/b] follow")
            yield OptionList(id="review-skill-items")
            later = "ask me next time" if self._asking else "cancel"
            yield Label(f"enter chooses · esc {later} · you can change it with i", classes="hint")

    def on_mount(self) -> None:
        options = []
        for skill in [*self._skills, Skill(name="", description="", likely=False)]:
            name = skill.name
            title = f"/{name}" if name else "Purr's own review"
            description = (
                skill.description
                if name
                else "Its general review, holding the PR to the repo's CLAUDE.md / AGENTS.md"
            )
            # Descriptions wrap in their own column, under the name.
            row = Table.grid(padding=(0, 1))
            row.add_column(width=1)
            row.add_column(ratio=1)
            row.add_row("✓" if name == self._chosen else "", Text(title, style="bold"))
            row.add_row("", Text(description, style="dim"))
            options.append(Option(row))
        items = self.query_one(OptionList)
        items.set_options(options)
        items.highlighted = (
            self._choices.index(self._chosen) if self._chosen in self._choices else 0
        )
        items.focus()

    @on(OptionList.OptionSelected, "#review-skill-items")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._choices[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)
