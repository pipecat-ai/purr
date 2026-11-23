"""A repo's page: its PRs on the left (or a PR's diff), the selected PR's session on
the right."""

import asyncio
from functools import partial

from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from purr.messages import FetchDetails, GetDiff
from purr.models import PR, PRState
from purr.tui.diffview import DiffPane
from purr.tui.keys import REPO_KEYS
from purr.tui.lists import (
    DONE_SHOWN,
    GROUP_GAP,
    GROUPS,
    HEADING,
    ICONS,
    PRGroup,
    PRList,
    blank,
    group,
    heading,
    matches,
    pr_columns,
    pr_row,
    separated,
)
from purr.tui.pr_keys import PRKeys
from purr.tui.session import PRSession, SessionState, outcome_mark
from purr.tui.theme import palette
from purr.tui.working import FRAME_SECONDS, FRAMES

NEARBY = 10  # PRs below the cursor whose details are fetched ahead


class SearchBox(Input):
    """The PR list's search: enter or esc goes back to the list, the search kept."""

    BINDINGS = [
        Binding("enter", "submit", "Done"),
        Binding("escape", "leave", "Back"),
    ]

    async def action_leave(self) -> None:
        await self.action_submit()  # as enter (its own action, for the footer to show)


class RepoView(PRKeys):
    """A repo: its PRs grouped by state on the left, the selected PR's session on the right.

    One instance serves every repo; it remembers the selected PR per repo. What its keys
    do to a PR (review, approve, merge…) is in PRKeys.
    """

    # Its keys work from either pane; each pane binds the ones it shows first (pane_keys).
    BINDINGS = [
        Binding("escape,ctrl+g", "unmark", "Unmark"),  # while PRs are marked (first)
        Binding("escape", "clear_search", "Clear search"),  # while there's a search
        Binding("escape", "back", "Home"),
        *REPO_KEYS.values(),
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
        self._listed: str | None = None  # the repo whose PRs the list has
        self._folded: dict[str, set[str]] = {}  # repo -> its groups folded (←)

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="prs-pane"):
                yield SearchBox(id="search", placeholder="Search: number, title, author…")
                yield PRList(id="prs")
            yield DiffPane(id="diff-pane")  # in the list's place, while it's shown (d)
            yield PRSession(id="session")

    def on_mount(self) -> None:
        self.query_one("#prs-pane").border_title = "PRs"
        self.query_one("#search").display = False
        self.query_one(DiffPane).display = False
        self._left = "list"  # what's on the left: the PR list, or a PR's diff
        self._zoomed_from_left = False  # z on the PR's pane was pressed from the left one
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

    def _list(self, at: str | None = None) -> None:
        """Draw the list: the PRs matching the search, grouped; the cursor ``at`` an option
        (by id), or else on the selected PR (or its folded group), or else the first PR."""
        prs = [pr for pr in self._all if matches(pr, self._search)]
        groups = self._groups(prs)
        folded = self._folded_now()
        self._prs = {str(pr.ref): pr for g in groups if g.key not in folded for pr in g.prs}
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
        # Where you'd scrolled to (the wheel, say), for this repo's list drawn again.
        scrolled = option_list.scroll_offset.y if self._listed == self.repo else None
        self._listed = self.repo
        option_list.set_options(options)
        selected = self._selected.get(self.repo)
        if not at:  # the selected PR, or the heading of the group it's folded away in
            at = selected if selected in self._prs else self._fold_of(selected)
        if at:
            option_list.highlighted = option_list.get_option_index(at)
            if scrolled is not None:
                option_list.scroll_to(y=scrolled, animate=False, immediate=True)  # stays put
        else:
            pr = next((i for i, o in enumerate(options) if o.id in self._prs), None)
            first = next((i for i, o in enumerate(options) if not o.disabled), None)
            option_list.highlighted = first if pr is None else pr
        self._show_highlighted()

    def _groups(self, prs: list[PR]) -> list[PRGroup]:
        """The PRs in their groups; empty groups left out."""
        groups = []
        for key, icon, label, style in GROUPS:
            members = [pr for pr in prs if group(pr) == key]
            if not members:
                continue
            title = Text(f"{icon} {label} ({len(members)})", style=style)
            if key == PRState.DONE:
                members, note = self._latest_done(members)
                title.append(note, style="dim")
            groups.append(PRGroup(key, title, style, members))
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

    def _options(self, groups: list[PRGroup]) -> list[Option]:
        """The column headings, then each group's heading and its PRs (unless it's folded),
        a thin line between PRs."""
        options = [Option(pr_columns(self._number_width), disabled=True)]
        folded = self._folded_now()
        for pr_group in groups:
            if len(options) > 1:
                options += blank(GROUP_GAP)
            shut = pr_group.key in folded
            options.append(heading(pr_group, folded=shut))
            if not shut:
                options += separated(
                    [Option(self._prompt(pr), id=str(pr.ref)) for pr in pr_group.prs]
                )
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
        unread = str(pr.ref) in self.purr_app.alerts
        return pr_row(pr, self._mark(pr), unread=unread, number_width=self._number_width)

    def _mark(self, pr: PR) -> Text:
        """Marked; its agent at work (spinning); what its review found; or its group's."""
        if str(pr.ref) in self._marked:
            return Text("▪", style="bold magenta")
        if str(pr.ref) in self._spinning:
            return Text(FRAMES[self._frame % len(FRAMES)], style=f"bold {palette().working}")
        outcome = self._outcomes.get(pr.ref) if pr.state != PRState.REVIEWING else None
        return outcome_mark(outcome) if outcome else Text(ICONS[group(pr)])

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
        elif listed := next((p for p in self._all if str(p.ref) == pr), None):
            self._folded.get(repo, set()).discard(group(listed))  # its group opens
            self._list()  # or an older done one: listed now that it's selected

    @on(OptionList.OptionHighlighted)
    def _show_highlighted(self) -> None:
        self.refresh_bindings()  # tab: fold or open on a heading, the PR's pane on a PR
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

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "open":  # in the PR's pane, it's already open
            return not self.session.has_focus_within
        if action in ("search", "move", "mark") and self.diff_shown:  # the list's away
            return False
        if action == "unmark":  # esc: the marks go first, then the search, then home
            return bool(self._marked) and not self.diff_shown
        if action == "clear_search":
            return bool(self._search) and not self.diff_shown
        if action == "search":  # while typing, / is a slash
            return not self.session.query_one("#prompt").has_focus
        return True

    def action_open(self) -> None:
        """Into the selected PR's pane, to read it (o)."""
        if self.selected_pr():
            self.session.focus_transcript()

    @on(OptionList.OptionSelected)
    def _ask(self, event: OptionList.OptionSelected) -> None:
        """enter on a PR: straight to its prompt, to ask the agent about it. On a group's
        heading (or a click on it): it folds or opens."""
        if self.selected_pr():
            self.session.focus_prompt()
        elif event.option_list.id == "prs":
            self.action_toggle_group()

    def action_switch_pane(self) -> None:
        """From the list (or its search, or the diff) to the PR's pane, and back."""
        left = self.diff if self.diff_shown else self.query_one("#prs-pane")
        if not self.session.has_focus_within:
            if self.session.pr:  # the selected PR, or the last one (on a heading)
                self.session.focus_transcript()
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
        self.refresh_bindings()  # esc: clear the search, or home

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
        self.refresh_bindings()  # esc: clear the search, or home

    @on(Input.Submitted, "#search")
    def _searched(self) -> None:
        self._leave_search()

    def action_clear_search(self) -> None:
        """esc with a search on, from the list or the PR's pane: it goes, and the list
        has every PR again, the cursor on the one it was on. esc again goes home."""
        self._set_search("")
        self._list()

    async def action_back(self) -> None:
        """Home, from the list or the PR's pane (tab goes between them).

        From the search box, back to the list (the search stays, shown above it; esc
        clears it); from the prompt, back to the PR's pane (see PRSession): esc again goes
        home. Anything half-typed in the prompt is there when you're back.
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

    # -- Folding --------------------------------------------------------------------

    def _folded_now(self) -> set[str]:
        """The groups folded in this repo; none while searching, so every match shows."""
        return set() if self._search or not self.repo else self._folded.get(self.repo, set())

    def _fold_of(self, ref: str | None) -> str | None:
        """The heading of the folded group a PR is in, if it is in one."""
        pr = next((pr for pr in self._all if str(pr.ref) == ref), None)
        return f"{HEADING}{group(pr)}" if pr and group(pr) in self._folded_now() else None

    def on_heading(self) -> bool:
        """Whether the cursor's on a group's heading, to fold or open (not while searching:
        every match shows)."""
        return self._heading_here() is not None and not self._search

    def _heading_here(self) -> str | None:
        option = self.query_one(PRList).highlighted_option
        return option.id if option and option.id and option.id.startswith(HEADING) else None

    def action_toggle_group(self) -> None:
        """tab (or enter, or a click) on a group's heading folds or opens it."""
        heading_id = self._heading_here()
        if not (heading_id and self.repo and self.on_heading()):
            return
        self._folded.setdefault(self.repo, set()).symmetric_difference_update(
            {heading_id.removeprefix(HEADING)}
        )
        self._list(at=heading_id)  # the cursor stays on it

    # -- Moving and marking --------------------------------------------------------

    def action_move(self, delta: int) -> None:
        """The next or previous PR, from either pane, over the groups' headings."""
        option_list = self.query_one(PRList)
        if (here := option_list.highlighted) is None:
            return
        ids = [option.id for option in option_list.options]
        ahead = range(here + delta, len(ids) if delta > 0 else -1, delta)
        found = next((i for i in ahead if ids[i] in self._prs), None)
        if found is not None:
            option_list.highlighted = found

    def action_unmark(self) -> None:
        """esc (or ctrl+g) with PRs marked: none are."""
        self._marked.clear()
        self._list()
        self.refresh_bindings()

    def action_mark(self) -> None:
        pr = self.selected_pr()
        if not pr:
            return
        self._marked ^= {str(pr.ref)}
        self.refresh_bindings()  # esc: unmark, while some are
        option_list = self.query_one(OptionList)
        index = option_list.highlighted
        option_list.replace_option_prompt(str(pr.ref), self._prompt(pr))
        if index is not None and index + 1 < option_list.option_count:
            option_list.action_cursor_down()

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
        self.diff.remove_class("-wide")
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
        self.diff.remove_class("-wide")
        self.session.ask_about(event.quote)

    async def restyle(self) -> None:
        """Drawn again, in another theme's colors: the list, the PR's pane, the diff."""
        if self.repo:
            self._list()
        if self.session.pr is not None:
            self.session.redraw(keep_place=True)
            await self.session.refresh_status()
        if self.diff_shown:
            self.diff.view.show(self.diff.view.comments)

    # -- The panes -----------------------------------------------------------------

    def action_thinking(self) -> None:
        """The agents' reasoning in full, or a line per thought."""
        app = self.purr_app
        app.full_thinking = not app.full_thinking
        if self.session.pr is not None:
            self.session.redraw(keep_place=True)
        what = "in full" if app.full_thinking else "a line per thought"
        self.notify(f"The agent's reasoning: {what}", timeout=2)

    def action_zoom(self) -> None:
        """Full screen: the diff, from it, or else the PR's pane; and back, the focus where
        it was before."""
        if self.diff_shown and not self.session.has_focus_within:
            self.session.display = not self.session.display
            self.diff.set_class(not self.session.display, "-wide")  # all the width, zoomed
            return
        left = self.diff if self.diff_shown else self.query_one("#prs-pane")
        if left.display:  # zooming the PR's pane: from it, or from the list
            self._zoomed_from_left = not self.session.has_focus_within
            left.display = False
            if self._zoomed_from_left:
                self.session.focus_transcript()
        else:  # and back, the focus where it was
            left.display = True
            if self._zoomed_from_left:
                self._focus_left()
