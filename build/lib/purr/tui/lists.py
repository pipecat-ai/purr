"""Purr's lists: things in groups under headings (the PRs, the repos), and how a PR
shows in one."""

from typing import TYPE_CHECKING, Literal, NamedTuple, cast

from rich.color import Color
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual._widget_navigation import find_next_enabled_no_wrap
from textual.binding import Binding
from textual.strip import Strip
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from purr.models import PR, PRState
from purr.tui.keyhelp import KEYS
from purr.tui.keys import pane_keys
from purr.tui.overview import short_age
from purr.tui.session import number_style
from purr.tui.theme import palette

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

OTHERS = "others"
REQUESTED = "requested"
YOURS = "yours"
UP_NEXT = "upnext"
# What's yours first: what needs you, what's under way, your own PRs (when someone
# comments on one, it needs you), what's up next (the pool's PRs worth looking at
# next, best first), PRs asking for your review (someone's waiting on you), and
# what you finished lately (the latest few; search finds the rest). Then what
# others are reviewing, and last the pool of unreviewed PRs (hundreds, in a big
# repo).
GROUPS = [
    (PRState.NEEDS_YOU, "⚠", "NEEDS YOU", "bold yellow"),
    (PRState.REVIEWING, "●", "REVIEWING", "bold cyan"),
    (YOURS, "◆", "YOUR PRS", "bold blue"),
    (UP_NEXT, "➜", "UP NEXT", "bold green"),
    (REQUESTED, "◇", "REVIEW REQUESTED", "bold magenta"),
    (PRState.DONE, "✓", "DONE", "bold green"),
    (OTHERS, "◌", "OTHERS REVIEWING", "bold"),
    (PRState.NOT_REVIEWED, "○", "NOT REVIEWED", "bold"),
]
DONE_SHOWN = 5
ICONS = {key: icon for key, icon, *_ in GROUPS}


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
        while index and self._heading(self.get_option_at_index(index - 1)):
            index -= 1
        return index

    def _heading(self, option: Option) -> bool:
        """A heading (or the gap above one), which comes into view with what's under it."""
        return option.disabled

    def render_line(self, y: int) -> Strip:
        """A line, with any text in about the cursor's own color (a PR's number, in the
        theme's primary) in the cursor's text color instead, so it doesn't vanish."""
        strip = super().render_line(y)
        if not any(_hidden(segment.style) for segment in strip):
            return strip
        cursor = self.get_visual_style("option-list--option", "option-list--option-highlighted")
        legible = Style(color=cursor.rich_style.color)
        return Strip(
            [
                Segment(text, style + legible, control) if _hidden(style) else segment
                for segment in strip
                for text, style, control in [segment]
            ],
            strip.cell_length,
        )


def _hidden(style: Style | None) -> bool:
    """Text about the color of what's behind it."""
    if not style or not style.color or not style.bgcolor:
        return False
    return _close(style.color, style.bgcolor)


def _close(one: Color, other: Color) -> bool:
    if one.triplet is None or other.triplet is None:  # the terminal's colors: as it has them
        return one == other
    return sum((a - b) ** 2 for a, b in zip(one.triplet, other.triplet, strict=True)) < 40**2


class PRList(GroupedList):
    """The PR list, which can redraw one row without rebuilding the rest.

    ``replace_option_prompt`` lays out every row again: fine once, but a spinning
    mark redraws several times a second (300 PRs: most of a CPU).
    """

    # On a group's heading, tab folds or opens it; on a PR, tab goes to the PR's pane.
    BINDINGS = [
        KEYS,
        *pane_keys(
            ["search", "review", "skip", "mark", "approve", "dismiss", "merge", "diff", "zoom"]
            + ["shell", "open_claude"]
        ),
        Binding("tab", "repo('toggle_group')", "Fold/open"),  # first, where it applies
        *pane_keys(["switch_pane"], switch_pane="PR"),
    ]

    async def action_repo(self, action: str) -> None:
        await cast("PurrApp", self.app).main.repo.run_action(action)

    def _heading(self, option: Option) -> bool:
        return option.disabled or bool(option.id and option.id.startswith(HEADING))

    def check_action(self, action: str, parameters: tuple) -> bool:
        """On a heading, tab folds or opens it (shift+tab's the other pane still, through
        the repo view's keys); elsewhere tab's the other pane, as everywhere."""
        if parameters == ("toggle_group",):
            return cast("PurrApp", self.app).main.repo.on_heading()
        if parameters == ("switch_pane",):
            return not cast("PurrApp", self.app).main.repo.on_heading()
        if parameters == ("skip",):
            return cast("PurrApp", self.app).main.repo.on_up_next()
        if parameters == ("approve",):
            return bool(cast("PurrApp", self.app).main.repo.a_does())
        return True

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
    """Where a PR goes in the list: its review state; or "yours" if you opened it and
    nothing's happening; or, not reviewed, "requested" if it asks for your review,
    "others" if someone else took it."""
    if pr.authored and pr.state in (PRState.NOT_REVIEWED, PRState.DONE):
        return YOURS
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
    gaps = [1, GAP, GAP, GAP, 1, 1]  # after each; the last keeps the date off the scrollbar
    grid = Table.grid(expand=True)
    row: list[Text | str] = []
    for column, cell, gap in zip(columns, cells, gaps, strict=True):
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


def pr_columns(number_width: int) -> Table:
    """The PR list's column headings."""
    headings = ("", "#", "TITLE", "ASSIGNED", "AUTHOR", "OPENED")
    return _row(*(Text(h, style="dim") for h in headings), number_width=number_width)


def blank(lines: int = 1) -> list[Option]:
    """Empty lines in a list, which the cursor skips."""
    return [Option(" ", disabled=True) for _ in range(lines)]


def separated(rows: list[Option]) -> list[Option]:
    """Rows with a thin line between one and the next, so the list isn't crowded."""
    out: list[Option] = []
    for row in rows:
        out += [_line(), row] if out else [row]
    return out


def _line() -> Option:
    return Option(Rule(style="dim"), disabled=True)


GROUP_GAP = 2  # empty lines between groups: after a group's last row, more than between rows
HEADING = "group:"  # a group heading's id: "group:done"


class PRGroup(NamedTuple):
    """A group in the PR list: its key (see ``group``), title, colour and listed PRs."""

    key: str
    title: Text
    style: str
    prs: list[PR]


def heading(pr_group: PRGroup, *, folded: bool) -> Option:
    """A group's heading: ▾ (▸ when folded), its title, and a line in its colour to the edge.

    It takes the cursor, like a file in the diff, for tab to fold or open it.
    """
    title = Text("▸ " if folded else "▾ ", style=pr_group.style) + pr_group.title
    return Option(titled_line(title, pr_group.style), id=f"{HEADING}{pr_group.key}")


def titled_line(title: Text, style: str) -> Rule:
    """A heading in a list: its title, then a double line in its colour to the edge (the
    rows between have a thin one)."""
    return Rule(title, characters="═", align="left", style=style)


def pr_row(pr: PR, mark: Text, *, unread: bool, number_width: int, note: str = "") -> Table:
    """A PR in the list: its mark, number, title (and ``note``, dim, after it: why it's up
    next), assignees, author and age."""
    # Something happened since you last looked: like unread mail.
    title = Text(pr.title, style=f"bold {palette().name}" if unread else palette().name)
    if note:
        title.append(f"  {note}", style="dim")
    return _row(
        mark,
        Text(f"#{pr.number}", style=number_style()),
        title,
        _assignees(pr),
        Text(pr.author),
        Text(short_age(pr.created_at)),
        number_width=number_width,
    )
