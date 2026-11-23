"""The diff (``d``): a PR's diff in the PR list's place, to read line by line, ask
about and comment on, next to the PR's conversation. ``z`` makes it full screen;
``esc`` or ``d`` closes it.

Files start as a line each (all of them opened if the diff is short; those with
comments on them, if not): ``tab`` opens or closes the one the cursor's in, ``e``
all of them, ``n``/``p`` go to the next or previous file. ``shift+↑``/``shift+↓``
(or ``ctrl+space`` and moving) select lines; ``enter`` asks the agent about them
(or the line under the cursor), in the PR's conversation. ``c`` writes a comment
on them, or rewrites yours under the cursor, and ``x`` removes it.

Yours are posted with your next review of the PR (see DraftComment). The
comments the agent proposes show too, and the PR's review threads on GitHub:
each in a box under its line, which ``tab`` closes to a line (resolved threads
start that way) and opens again.
"""

import re
import textwrap
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Label

from purr.actions import unsigned
from purr.diff import File, Line, parse
from purr.jobs import PurrError, Request
from purr.messages import (
    EditGitHubComment,
    EditProposedComment,
    GetReviewThreads,
    RemoveComment,
    ReplyToThread,
    SaveComment,
)
from purr.models import PR, DraftComment, Quote
from purr.tui.keyhelp import KEYS
from purr.tui.keys import pane_keys
from purr.tui.overview import short_age
from purr.tui.prompt import PromptArea

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_WRAP = 100  # a comment's lines, at most this wide
SHORT_DIFF = 60  # a diff this short starts with its files opened


def opens_short(diff: str | None) -> set[str]:
    """The files that start opened: all of a short diff's, none of a longer one's."""
    if diff is None or len(diff.splitlines()) > SHORT_DIFF:
        return set()
    return {file.path for file in parse(diff)}


def file_line(file: File, opened: bool) -> Text:
    """``▸ src/bus.py  +22 −4``: a file in the diff, opened or not."""
    return Text.assemble(
        ("▾ " if opened else "▸ ", "dim"),
        (file.path, "bold"),
        (f"  +{file.additions}", "green"),
        (f" −{file.deletions}", "red"),
        (f"  {file.note}" if file.note else "", "dim italic"),
    )


# How git colors a diff's lines, by how they start.
LINE_STYLES = {"@@": "cyan", "+": "green", "-": "red", "\\": "dim"}


@dataclass(frozen=True)
class Said:
    """A comment on a line, a box of its own: yours, not posted yet (``mine``), one the
    agent proposes, or one in a review thread on GitHub (``thread``, to reply in; ``reply``
    if it isn't the thread's first; ``yours`` if you wrote it, to change it there)."""

    path: str
    line: int  # 0: on no line of this diff (outdated)
    side: str
    who: str
    text: str
    source: str  # "you", "agent", "github"
    key: str  # which it is, to open or close it
    resolved: bool = False
    mine: DraftComment | None = None
    start: int | None = None  # on several lines: the first (line is the last)
    thread: str = ""
    reply: bool = False
    yours: bool = False
    id: str = ""  # on GitHub
    at: str = ""  # when it was written (ISO)


@dataclass(frozen=True)
class Row:
    """A line of the view: a file's, one of its diff lines, a comment's, or a note."""

    kind: str  # "file", "line", "said", "note"
    text: Text
    file: int  # which file it's in
    line: Line | None = None
    said: Said | None = None

    def key(self) -> tuple:
        """What it shows, to find it again once the rows change (a comment by its key:
        rewriting it doesn't make it another)."""
        return (self.kind, self.file, self.line, self.said.key if self.said else None)


def said(
    mine: Sequence[DraftComment],
    proposed: Sequence[dict],
    threads: Sequence[dict] = (),
    proposed_at: str = "",
) -> list[Said]:
    """Your comments, the agent's proposed ones (``proposed_at``), and the PR's review
    threads on GitHub."""
    yours = [_yours(comment) for comment in mine]
    theirs = [
        Said(str(c["path"]), int(c["line"]), "RIGHT", "agent", str(c.get("body") or ""),
             "agent", f"agent:{i}", at=proposed_at)
        for i, c in enumerate(proposed)
        if c.get("path") and c.get("line")
    ]  # fmt: skip
    on_github = [said for thread in threads if thread.get("path") for said in _thread(thread)]
    return yours + theirs + on_github


def _yours(comment: DraftComment) -> Said:
    start = comment.start_line if comment.start_line != comment.line else None
    where = (comment.path, comment.line, comment.side)
    key = f"you:{comment.id}"
    return Said(*where, "you", comment.body, "you", key, mine=comment, start=start,
                at=comment.created_at)  # fmt: skip


def _thread(thread: dict) -> list[Said]:
    """A review thread on GitHub: a box for each of its comments, in order."""
    where = (str(thread["path"]), int(thread.get("line") or 0), str(thread.get("side") or "RIGHT"))
    return [
        Said(*where, comment.get("author") or "?", comment.get("body") or "", "github",
             f"github:{thread.get('id')}:{i}", resolved=bool(thread.get("resolved")),
             thread=str(thread.get("id")), reply=i > 0, yours=bool(comment.get("by_user")),
             id=str(comment.get("id") or ""), at=str(comment.get("at") or ""))
        for i, comment in enumerate(thread["comments"])
    ]  # fmt: skip


def rows(
    files: Sequence[File], opened: set[int], comments: Sequence[Said], shut: set[str] = frozenset()
) -> list[Row]:
    """The view's lines: each file's, then, if it's opened, its lines and their comments
    (those in ``shut`` as a line each)."""
    out: list[Row] = []
    for index, file in enumerate(files):
        on_file = [c for c in comments if c.path == file.path]
        out.append(Row("file", _file_text(file, index in opened, on_file), index))
        if index in opened:
            out += _file_rows(index, file, on_file, shut)
    return out


def _file_text(file: File, opened: bool, comments: list[Said]) -> Text:
    text = file_line(file, opened)
    if comments:
        text.append(f"  ✎ {len(comments)}", style="yellow")
    return text


def _file_rows(index: int, file: File, comments: list[Said], shut: set[str]) -> list[Row]:
    if file.generated:
        return [Row("note", Text("  generated, not shown", style="dim"), index)]
    out: list[Row] = []
    for line in file.lines:
        out.append(Row("line", _numbered(line), index, line))
        for comment in (c for c in comments if (c.line, c.side) == line.target()):
            out += _said_rows(index, comment, shut)
    placed = {line.target() for line in file.lines}
    for comment in (c for c in comments if (c.line, c.side) not in placed):
        out += _said_rows(index, comment, shut, where=True)
    return out


def _numbered(line: Line) -> Text:
    """A diff line with its numbers: in the base, and in the PR's version."""
    gutter = f"{line.old or '':>5}{line.new or '':>5}  "
    return Text.assemble((gutter, "dim"), (line.text.expandtabs(4), LINE_STYLES.get(line.kind, "")))


_INDENT = " " * 12  # under the code, past the line numbers
_COLORS = {"you": "yellow", "agent": "magenta", "github": "cyan"}


def _said_rows(index: int, said: Said, shut: set[str], *, where: bool = False) -> list[Row]:
    """A comment under its line: in a box, or (shut) a line saying who and how it starts."""
    texts = [_shut_line(said, where)] if said.key in shut else _box(said, where)
    return [Row("said", text, index, said=said) for text in texts]


def _title(said: Said, where: bool) -> str:
    """Who wrote it and when, and what else to know: ``alice · 3h ago · reply · resolved``,
    ``agent, proposed · now``, ``you · lines 4–9`` (and where, if it isn't on a line here)."""
    title = "agent, proposed" if said.source == "agent" else said.who
    title += " (you)" if said.yours else ""
    notes = [_when(said.at)] if said.at else []
    notes += [f"lines {said.start}–{said.line}"] if said.start else []
    notes += ["reply"] if said.reply else []
    notes += ["resolved"] if said.resolved else []
    notes += [_where(said)] if where else []
    return " · ".join([title, *notes])


def _when(at: str) -> str:
    """``3h ago``, ``2d ago``, ``now``."""
    age = short_age(at)
    return age if age == "now" else f"{age} ago"


def _where(said: Said) -> str:
    return f"line {said.line}, not in the diff" if said.line else "outdated"


def _shut_line(said: Said, where: bool) -> Text:
    """A shut box: its top line, who wrote it and when, and how it starts."""
    color = _COLORS[said.source]
    return Text.assemble(
        (f"{_indent(said)}╶─ ▸ ", color),
        (_title(said, where), f"bold {color}"),
        (" ─ ", color),
        (_short(said.text), "dim" if said.resolved else ""),
        (" ─╴", color),
    )


def _short(text: str, width: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


_INSIDE = "on #073642"  # a box's inside: Solarized's highlighted background
_MENTION = re.compile(r"(?<![\w/])@[A-Za-z0-9][\w-]*(?:/[\w.-]+)?")  # @alice, @org/team


def _box(said: Said, where: bool) -> list[Text]:
    """The comment in a box, wrapped: its border and title (who wrote it) in its color
    (yours, the agent's, GitHub's), resolved or not; the words in the text's, with
    mentions picked out."""
    color = _COLORS[said.source]
    body = _wrapped(said.text)
    title = f"▾ {_title(said, where)}"
    width = max([line.cell_len for line in body] + [cell_len(title) + 2])
    indent = _indent(said)
    top = Text.assemble(
        (f"{indent}╭─ ", color),
        (title, f"bold {color}"),
        (f" {'─' * (width - cell_len(title) - 1)}╮", color),
    )
    middle = [_inside(line, width, color, indent) for line in body]
    bottom = Text(f"{indent}╰{'─' * (width + 2)}╯", style=color)
    return [top, *middle, bottom]


def _indent(said: Said) -> str:
    """Under the code (a reply too: it's on the same line, and its title says so)."""
    return _INDENT


def _inside(line: Text, width: int, color: str, indent: str) -> Text:
    """A line of a box: between its sides, on its background, padded to its width."""
    inside = Text(" ", style=_INSIDE)
    inside.append_text(line)
    inside.append(" " * (width - line.cell_len + 1), style=_INSIDE)
    return Text.assemble((f"{indent}│", color), inside, ("│", color))


def _wrapped(text: str) -> list[Text]:
    """A comment's lines, wrapped, with its mentions picked out."""
    lines = [
        line for paragraph in text.splitlines() for line in textwrap.wrap(paragraph, _WRAP) or [""]
    ] or [""]
    return [_mentions(line) for line in lines]


def _mentions(line: str) -> Text:
    """A line with its @mentions in bold blue."""
    text = Text(line, style=_INSIDE)
    text.highlight_regex(_MENTION, "bold blue")
    return text


def first_opened(files: Sequence[File], diff: str, comments: Sequence[Said]) -> set[int]:
    """The files opened at first: all of a short diff, else those with comments."""
    short = opens_short(diff)
    commented = {c.path for c in comments}
    return {i for i, file in enumerate(files) if file.path in short or file.path in commented}


_SELECT = Binding.Group("Select", compact=True)


class DiffView(ScrollView, can_focus=True):
    """The diff's lines, a cursor on one, and a selection; long lines scroll sideways.

    ``shift+↑``/``shift+↓`` select, as in most editors; ``ctrl+space`` sets a mark that
    moving selects from, as in Emacs. ``esc`` (or ``ctrl+g``) lets the selection go.
    """

    COMPONENT_CLASSES = {"diff-view--cursor", "diff-view--selected"}
    TAKES_OVER = {"next_alert", "command_palette"}  # the app's ctrl+n/p: they move here
    DEFAULT_CSS = """
    DiffView { height: 1fr; }
    DiffView > .diff-view--cursor { background: $accent 30%; }
    DiffView > .diff-view--selected { background: $accent 15%; }
    """
    BINDINGS = [
        Binding("up,k,ctrl+p", "move(-1)", "Up", show=False),
        Binding("down,j,ctrl+n", "move(1)", "Down", show=False),
        Binding("pageup,alt+v", "page(-1)", "Page up", show=False),  # alt+v, ctrl+v: as in Emacs
        Binding("pagedown,ctrl+v", "page(1)", "Page down", show=False),
        Binding("home", "move(-1000000)", "Top", show=False),
        Binding("end", "move(1000000)", "Bottom", show=False),
        Binding("shift+up", "select(-1)", "Select up", group=_SELECT),
        Binding("shift+down", "select(1)", "Select down", group=_SELECT),
        Binding("ctrl+space,ctrl+@", "mark", "Mark", show=False),
        Binding("escape,ctrl+g", "unselect", "Unselect"),  # only while selecting
        # Here rather than on the pane: tab would move the focus otherwise.
        Binding("tab,space", "toggle_here", "Open/close", key_display="tab"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.load(())

    def load(self, files: Sequence[File]) -> None:
        """Another diff: nothing opened, said or selected yet."""
        self.files = files
        self.opened: set[int] = set()
        self.comments: list[Said] = []
        self.shut: set[str] = set()  # the comments shown as a line (resolved ones, at first)
        self._known: set[str] = set()  # the comments seen so far
        self.cursor = 0
        self._here = (0, 0)  # the rows the cursor's on: its row, or a comment's box
        self._anchor: int | None = None
        self.marking = False  # the anchor's a mark (ctrl+space): moving keeps selecting
        self._rows: list[Row] = []
        self._strips: dict[int, Strip] = {}

    def show(self, comments: list[Said], opened: set[int] | None = None) -> None:
        """Lay the rows out again: other comments, other files opened. The cursor stays
        on the line it was on (or its file); a selection goes."""
        here = self.current()
        self.comments = comments
        new = [c for c in comments if c.key not in self._known]
        self.shut |= {c.key for c in new if c.resolved}
        self._known |= {c.key for c in new}
        if opened is not None:
            self.opened = opened
        self._rows = rows(self.files, self.opened, comments, self.shut)
        self._strips.clear()
        width = max((row.text.cell_len for row in self._rows), default=0)
        self.virtual_size = Size(width, len(self._rows))
        self.anchor, self.marking = None, False
        self._place(self._find(here))  # where you're looking stays: no jump to the cursor

    def current(self) -> Row | None:
        return self._rows[self.cursor] if 0 <= self.cursor < len(self._rows) else None

    def _find(self, row: Row | None) -> int:
        """Where a row is now; if it's gone (a comment removed), the row now where it was,
        if that's in the same file, else the file's."""
        if row is None:
            return 0
        same = next((i for i, r in enumerate(self._rows) if r.key() == row.key()), None)
        if same is not None:
            return same
        here = min(self.cursor, len(self._rows) - 1)
        return here if here >= 0 and self._rows[here].file == row.file else self._file_row(row.file)

    def _file_row(self, file: int) -> int:
        return next((i for i, r in enumerate(self._rows) if r.file == file), 0)

    # -- Opening and closing -------------------------------------------------------

    def toggle(self, file: int) -> None:
        self.show(self.comments, self.opened ^ {file})
        self._move_to(self._file_row(file))

    def action_toggle_here(self) -> None:
        """Open or close the comment under the cursor, or else the file it's in."""
        row = self.current()
        if row and row.said:
            self.toggle_said(row.said)
        elif row:
            self.toggle(row.file)

    def toggle_said(self, said: Said) -> None:
        self.shut ^= {said.key}
        self.show(self.comments)
        self._move_to(next(i for i, r in enumerate(self._rows) if r.said == said))

    def toggle_all(self) -> None:
        everything = set(range(len(self.files)))
        self.show(self.comments, set() if self.opened == everything else everything)

    # -- Moving and selecting ------------------------------------------------------

    def next_file(self, step: int) -> None:
        """The cursor to the next (or previous) file's line."""
        starts = [i for i, row in enumerate(self._rows) if row.kind == "file"]
        later = [i for i in starts if (i - self.cursor) * step > 0]
        if later:
            self._go(min(later) if step > 0 else max(later))

    def action_move(self, step: int) -> None:
        """Up or down a row; a comment's box is one, however many lines it takes."""
        first, last = self._unit(self.cursor)
        self._go(last + step if step > 0 else first + step)

    def _unit(self, index: int) -> tuple[int, int]:
        """The rows moved over together: a comment's box (its rows are together), else the
        row itself."""
        said = self._rows[index].said if 0 <= index < len(self._rows) else None
        first = last = index
        while said and first > 0 and self._rows[first - 1].said == said:
            first -= 1
        while said and last + 1 < len(self._rows) and self._rows[last + 1].said == said:
            last += 1
        return first, last

    def action_page(self, step: int) -> None:
        self._go(self.cursor + step * max(self.size.height - 1, 1))

    def action_select(self, step: int) -> None:
        """Select up or down a line of code (over comments), in the file the cursor's in."""
        if self.anchor is None:
            self.anchor = self.cursor
        self._move_to(self._next_line(step))

    def _next_line(self, step: int) -> int:
        """The next line of code that way, in the cursor's file; where it is, if none."""
        here = self.current()
        rows = range(self.cursor + step, -1 if step < 0 else len(self._rows), step)
        lines = (i for i in rows if self._rows[i].line and here and self._rows[i].file == here.file)
        return next(lines, self.cursor)

    def action_mark(self) -> None:
        """Set the mark here (moving selects from it), or let it go."""
        self.marking = self.anchor is None
        self.anchor = self.cursor if self.marking else None
        self.refresh()

    @property
    def anchor(self) -> int | None:
        """Where the selection starts (the cursor's its other end); None: there's none."""
        return self._anchor

    @anchor.setter
    def anchor(self, line: int | None) -> None:
        if (line is None) != (self._anchor is None):
            self.refresh_bindings()  # esc: unselect while selecting, else close
        self._anchor = line

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Nothing selected, esc is the pane's: it closes the diff.
        return self.anchor is not None if action == "unselect" else True

    def action_unselect(self) -> None:
        self.anchor, self.marking = None, False
        self.refresh()

    def _go(self, index: int) -> None:
        """Move, letting a selection go, unless it's from a mark."""
        if not self.marking:
            self.anchor = None
        self._move_to(index)

    def _move_to(self, index: int) -> None:
        """The cursor to a row (a box's first, if it's in one), and all of it into view."""
        self._place(index)
        self._into_view(*self._here)

    def _place(self, index: int) -> None:
        """The cursor on a row (a box's first, if it's in one), wherever the view is."""
        index = min(max(index, 0), max(len(self._rows) - 1, 0))
        self._here = self._unit(index)
        self.cursor = self._here[0]
        self.refresh()

    # The mouse wheel scrolls; the cursor comes along, so keys act where you look.
    def on_mouse_scroll_down(self) -> None:
        self.call_after_refresh(self._cursor_in_view)

    def on_mouse_scroll_up(self) -> None:
        self.call_after_refresh(self._cursor_in_view)

    def _cursor_in_view(self) -> None:
        """After the wheel: an out of view cursor onto the first (or last) row in view."""
        top, height = round(self.scroll_offset.y), self.size.height
        bottom = min(top + height, len(self._rows)) - 1
        if self.cursor < top:
            first, last = self._unit(top)
            self._place(first if first >= top else last + 1)
        elif height and self._here[1] > bottom:
            self._place(self._unit(bottom)[0])

    def _into_view(self, first: int, last: int) -> None:
        top, height = self.scroll_offset.y, self.size.height
        if first < top:
            self.scroll_to(y=first, animate=False)
        elif height and last >= top + height:
            self.scroll_to(y=min(first, last - height + 1), animate=False)

    def selected(self) -> list[Line]:
        """The diff lines selected (those of the cursor's file), or else the one under it."""
        row = self.current()
        if row is None:
            return []
        if self.anchor is None:
            return [row.line] if row.line and row.line.target() else []
        low, high = sorted((self.anchor, self.cursor))
        chosen = (r.line for r in self._rows[low : high + 1] if r.file == row.file)
        return [line for line in chosen if line and line.target()]

    def quote(self) -> Quote | None:
        """What's selected, as a quote of the diff; None if it isn't on a line of it."""
        row, lines = self.current(), self.selected()
        if row is None or not lines:
            return None
        (start, start_side), (end, side) = lines[0].target(), lines[-1].target()  # type: ignore[misc]
        text = "\n".join(line.text for line in lines)
        return Quote(self.files[row.file].path, start, start_side, end, side, text)

    def on_click(self, event: events.Click) -> None:
        """A click puts the cursor on a line (shift: selects up to it); on a file's line, it
        opens or shuts the file."""
        offset = event.get_content_offset(self)
        index = self.scroll_offset.y + offset.y if offset else len(self._rows)
        if index >= len(self._rows):
            return
        if event.shift and self.anchor is None:
            self.anchor = self.cursor
        (self._move_to if event.shift else self._go)(index)
        if self._rows[index].kind == "file" and not event.shift:
            self.toggle(self._rows[index].file)

    # -- Drawing ---------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        index = self.scroll_offset.y + y
        width = self.size.width
        if index >= len(self._rows):
            return Strip.blank(width, self.rich_style)
        # The view's colors under the line's own; the cursor's, or the selection's, across
        # the whole row.
        style = self.rich_style + self._highlight(index)
        x = self.scroll_offset.x
        return self._strip(index).apply_style(style).crop_extend(x, x + width, style)

    def _highlight(self, index: int) -> Style:
        first, last = self._here  # the cursor's row, or its box
        if first <= index <= last:
            return self._background("diff-view--cursor")
        if (
            self.anchor is not None
            and min(self.anchor, self.cursor) < index < max(self.anchor, self.cursor) + 1
        ):
            return self._background("diff-view--selected")
        return Style()

    def _strip(self, index: int) -> Strip:
        if index not in self._strips:
            text = self._rows[index].text
            self._strips[index] = Strip(list(text.render(self.app.console, end="")))
        return self._strips[index]

    def _background(self, component: str) -> Style:
        return Style(bgcolor=self.get_component_rich_style(component).bgcolor)


class CommentScreen(ModalScreen[str | None]):
    """Write a comment on a line: enter saves, ctrl+j starts a new line, esc cancels."""

    DEFAULT_CSS = """
    CommentScreen { align: center middle; }
    #comment { width: 84; }
    #comment-body { height: 6; margin: 1 0; }
    """
    BINDINGS = [Binding("escape,ctrl+g", "cancel", "Cancel")]  # ctrl+g: as in Emacs

    def __init__(self, heading: str, body: str = ""):
        super().__init__()
        self._heading = heading  # "Your comment on src/bus.py:42"
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="comment"):
            yield Label(self._heading, classes="hint")
            yield PromptArea(id="comment-body")
            yield Label(
                "enter saves · ctrl+j new line · esc cancels · Emacs keys: ctrl+a/e/b/f, alt+b/f, "
                "ctrl+k/y…",
                classes="hint",
            )

    def on_mount(self) -> None:
        body = self.query_one(PromptArea)
        body.placeholder = "What should the author know about this line?"
        body.set_text(self._body)
        body.focus()

    @on(PromptArea.Submitted)
    def _saved(self, event: PromptArea.Submitted) -> None:
        event.stop()
        self.dismiss(event.text.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


_FILES = Binding.Group("Files", compact=True)


class DiffPane(Vertical):
    """A PR's diff, in place of the PR list (see the module's docstring). It says when
    you ask about lines of it (Asked) and when you close it (Closed); the repo view
    decides what that does."""

    @dataclass
    class Asked(Message):
        quote: Quote

    class Closed(Message):
        pass

    BINDINGS = [
        KEYS,
        Binding("enter", "ask", "Ask"),
        Binding("c", "comment", "Comment"),
        Binding("x", "remove", "Remove"),
        Binding("e", "toggle_all", "All"),
        Binding("n", "next_file(1)", "Next file", group=_FILES),
        Binding("p", "next_file(-1)", "Previous file", group=_FILES),
        Binding("escape,d", "close", "Close"),  # selecting, esc unselects: d, then
        *pane_keys(["zoom", "approve", "merge"]),
        Binding("shift+tab", "repo('switch_pane')", "Review", key_display="⇧tab"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pr: PR | None = None
        self._diff = ""
        self._files: tuple[File, ...] = ()
        self._threads: list[dict] = []  # the PR's review threads on GitHub, once read

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def compose(self) -> ComposeResult:
        yield DiffView(id="diff")

    @property
    def view(self) -> DiffView:
        return self.query_one(DiffView)

    async def show(self, pr: PR, diff: str) -> None:
        """The PR's diff at its head, with what's been said on it."""
        self.pr, self._diff, self._files, self._threads = pr, diff, parse(diff), []
        self.border_title = f"Diff #{pr.number}"
        self.view.load(self._files)
        comments = await self._comments()
        self.view.show(comments, first_opened(self._files, diff, comments))
        self.run_worker(self._load_threads(), group="threads", exclusive=True)

    async def reload(self) -> None:
        """What's been said changed (yours, the agent's proposal)."""
        if self.pr:
            self.view.show(await self._comments())

    async def _load_threads(self) -> None:
        """The PR's review threads on GitHub (a request away): their files open as they come,
        unless all of them are resolved."""
        assert self.pr is not None
        try:
            reply = await self.purr_app.bus.request(GetReviewThreads(pr=self.pr.ref))
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        self._threads = reply.threads
        comments = await self._comments()
        open_on = {c.path for c in comments if c.source == "github" and not c.resolved}
        on = {i for i, file in enumerate(self._files) if file.path in open_on}
        self.view.show(comments, self.view.opened | on)

    async def _comments(self) -> list[Said]:
        """Yours on this commit, those the agent proposes posting, and the PR's threads."""
        assert self.pr is not None
        store = self.purr_app.purr.store
        head = self.pr.head_sha
        mine = [c for c in await store.draft_comments(self.pr.ref) if c.head_sha == head]
        actions = await store.pending_actions(self.pr.ref)
        proposed = actions[0].payload.get("comments") or [] if actions else []
        return said(mine, proposed, self._threads, actions[0].created_at if actions else "")

    async def action_repo(self, action: str) -> None:
        await self.purr_app.main.repo.run_action(action)

    def action_close(self) -> None:
        self.post_message(self.Closed())

    def action_toggle_all(self) -> None:
        self.view.toggle_all()

    def action_next_file(self, step: int) -> None:
        self.view.next_file(step)

    def action_ask(self) -> None:
        """On a comment's box, write on it (a shut one opens); else ask the agent about the
        lines selected, or the one under the cursor."""
        row = self.view.current()
        if row and row.said and self.view.anchor is None:
            self._write_on(row.said)
        elif quote := self.view.quote():
            self.post_message(self.Asked(quote))
        else:
            self.notify("Ask about lines of the diff: move to one (shift+↑/↓ select several).")

    def _write_on(self, said: Said) -> None:
        """The editor, on what's said here, with its words to change: yours (rewritten),
        the agent's proposed comment (rewritten: approving posts yours), or a thread on
        GitHub: the last comment, if it's yours (changed there when you save it), else your
        reply (posted there when you save it)."""
        if said.key in self.view.shut:
            self.view.toggle_said(said)
            return
        heading, body, save = self._editing(said)

        async def saved(text: str | None) -> None:
            if text:
                await save(said, text)

        self.app.push_screen(CommentScreen(heading, body), saved)

    def _editing(self, said: Said) -> tuple[str, str, Callable[[Said, str], Awaitable[None]]]:
        """What the editor says, what it starts with, and what saving it does."""
        where = f"{said.path}:{said.line}"
        if said.source == "you":
            return f"Your comment on {where}", said.text, self._rewrite_mine
        if said.source == "agent":
            return f"The agent's proposed comment on {where}", said.text, self._rewrite_proposed
        if said.yours and said.id:
            signature = self.purr_app.purr.config.signature
            heading = f"Your comment on GitHub, on {where} (saving changes it there)"
            return heading, unsigned(said.text, signature), self._edit_on_github
        return f"Your reply on GitHub, to {said.who} on {where}", "", self._reply

    async def _rewrite_mine(self, said: Said, text: str) -> None:
        await self._save(text, None, said.mine)

    async def _rewrite_proposed(self, said: Said, text: str) -> None:
        assert self.pr is not None
        index = int(said.key.partition(":")[2])
        await self._request(EditProposedComment(pr=self.pr.ref, index=index, body=text))

    async def _edit_on_github(self, said: Said, text: str) -> None:
        assert self.pr is not None
        await self._request(EditGitHubComment(pr=self.pr.ref, comment_id=said.id, body=text))
        self.notify("Changed on GitHub")
        self.run_worker(self._load_threads(), group="threads", exclusive=True)

    async def _reply(self, said: Said, text: str) -> None:
        assert self.pr is not None
        await self._request(ReplyToThread(pr=self.pr.ref, thread_id=said.thread, body=text))
        self.notify("Replied on GitHub")
        self.run_worker(self._load_threads(), group="threads", exclusive=True)

    def action_comment(self) -> None:
        """A comment on the lines selected (or the one under the cursor, or someone's
        comment's), or yours under it rewritten."""
        row = self.view.current()
        mine = row.said.mine if row and row.said else None
        quote = None if mine else self._quoted(row)
        if mine is None and quote is None:
            self.notify("Comments go on lines of the diff: move to one, then c.")
            return

        async def save(body: str | None) -> None:
            if body:
                await self._save(body, quote, mine)

        where = _where_text(quote) if quote else _mine_text(mine)
        heading = f"Your comment on {where}"
        self.app.push_screen(CommentScreen(heading, mine.body if mine else ""), save)

    def _quoted(self, row: Row | None) -> Quote | None:
        """What a comment would be on: the selection, or the line of the comment under the
        cursor."""
        if row and row.said and row.said.line and self.view.anchor is None:
            line, side = row.said.line, row.said.side
            return Quote(row.said.path, line, side, line, side, "")
        return self.view.quote()

    async def _save(self, body: str, quote: Quote | None, mine: DraftComment | None) -> None:
        assert self.pr is not None
        if mine:
            spot = dict(path=mine.path, line=mine.line, side=mine.side, comment_id=mine.id)
        else:
            assert quote is not None
            spot = dict(path=quote.path, line=quote.end, side=quote.side,
                        start_line=quote.start, start_side=quote.start_side)  # fmt: skip
        await self._request(
            SaveComment(pr=self.pr.ref, body=body, head_sha=self.pr.head_sha, **spot)
        )

    async def action_remove(self) -> None:
        row = self.view.current()
        if not (self.pr and row and row.said and row.said.mine):
            self.notify("x removes a comment of yours: move to one first.")
            return
        await self._request(RemoveComment(pr=self.pr.ref, comment_id=row.said.mine.id))

    async def _request(self, job: Request) -> None:
        try:
            await self.purr_app.bus.request(job)
        except PurrError as e:
            self.notify(str(e), severity="warning")
        await self.reload()


def _where_text(quote: Quote) -> str:
    """``src/bus.py:404–412`` (``, deleted`` if it's on deleted lines)."""
    lines = f"{quote.start}–{quote.end}" if quote.start != quote.end else str(quote.end)
    deleted = ", deleted" if quote.side == "LEFT" and quote.start_side == "LEFT" else ""
    return f"{quote.path}:{lines}{deleted}"


def _mine_text(mine: DraftComment | None) -> str:
    assert mine is not None
    first = mine.start_line or mine.line
    lines = f"{first}–{mine.line}" if first != mine.line else str(mine.line)
    return f"{mine.path}:{lines}"
