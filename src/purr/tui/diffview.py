"""The diff (``d``): a PR's diff in the PR list's place, to read line by line, ask
about and comment on, next to the PR's conversation. ``z`` makes it full screen;
``esc`` or ``d`` closes it.

Files start as a line each (all of them opened if the diff is short; those with
comments on them, if not): ``tab`` opens or closes the one the cursor's in, ``e``
all of them, ``n``/``p`` go to the next or previous change (a file, or an opened
file's hunk). ``shift+↑``/``shift+↓`` (or ``ctrl+space`` and moving) select lines;
``enter`` asks the agent about them (or the line under the cursor), in the PR's
conversation. ``c`` writes a comment on them, or rewrites yours under the cursor,
and ``x`` removes it.

Yours go to GitHub as you save them. The comments the agent proposes show too,
and the PR's review threads on GitHub: each in a box under its line, which ``tab``
closes to a line (resolved threads start that way) and opens again.

Lines added and removed have a green or red background, under their code in its
language's colors (see purr.tui.code). The rows are laid out in purr.tui.diffrows.

What the diff leaves out around its hunks (see purr.diff.gaps) is a row each, the
hunk's header (or a row after the last hunk): ``tab`` there shows those lines, from the
file as it is in the PR, and hides them again. They can be asked about, not commented
on: GitHub takes comments only on the diff's lines.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from rich.style import Style
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

from purr.diff import File, Gap, Line, parse
from purr.jobs import PurrError, Request
from purr.messages import (
    DeleteGitHubComment,
    DropProposedComment,
    EditGitHubComment,
    EditPRComment,
    EditProposedComment,
    GetFile,
    GetReviewThreads,
    PostLineComment,
    PostPRComment,
    PRDiff,
    RemoveComment,
    ReplyToThread,
    SaveComment,
)
from purr.models import PR, DraftComment, PRRef, Quote
from purr.tui.boxes import BOX_WIDTH
from purr.tui.dialogs import ConfirmScreen
from purr.tui.diffrows import CONVERSATION, INDENT, Row, Said, first_opened, moved, rows, said
from purr.tui.keyhelp import KEYS
from purr.tui.keys import pane_keys
from purr.tui.prompt import PromptArea
from purr.tui.scrolling import glide_to, going_to
from purr.tui.theme import palette

if TYPE_CHECKING:
    from purr.tui.app import PurrApp


def _diff_title(pr: PR, diff: PRDiff) -> str:
    """``Diff #6756``, and what's in it that isn't on GitHub yet, if anything."""
    if not diff.unpushed:
        return f"Diff #{pr.number}"
    if not diff.commits:
        return f"Diff #{pr.number} · rebased, not pushed yet"
    commits = f"{diff.commits} commit{'s' * (diff.commits != 1)}"
    return f"Diff #{pr.number} · with {commits} not pushed yet"


_SELECT = Binding.Group("Select", compact=True)


class DiffView(ScrollView, can_focus=True):
    """The diff's lines, a cursor on one, and a selection; long lines scroll sideways.

    ``shift+↑``/``shift+↓`` select, as in most editors; ``ctrl+space`` sets a mark that
    moving selects from, as in Emacs. ``esc`` (or ``ctrl+g``) lets the selection go.
    """

    _on_comment = False  # the cursor's on a comment (the footer says what r does there)

    class NeedsFile(Message):
        """A gap's lines are wanted, and they come from the file as it is in the PR: read
        it (into ``sources``), then :meth:`reveal` them."""

        def __init__(self, file: int, at: int):
            super().__init__()
            self.file, self.at = file, at

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
        Binding("shift+up", "select(-1)", "Select up", key_display="⇧↑", group=_SELECT),
        Binding("shift+down", "select(1)", "Select down", key_display="⇧↓", group=_SELECT),
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
        self.revealed: set[tuple[int, int]] = set()  # the gaps shown: (file, where it is)
        self.sources: dict[str, str] = {}  # the files as they are in the PR, once read

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
        self._laid_out_at = self._box_width()
        self._rows = rows(
            self.files, self.opened, comments, self.shut, self._laid_out_at,
            revealed=self.revealed, sources=self.sources,
        )  # fmt: skip
        self._strips.clear()
        width = max((row.text.cell_len for row in self._rows), default=0)
        self.virtual_size = Size(width, len(self._rows))
        self.anchor, self.marking = None, False
        self._place(self._find(here))  # where you're looking stays: no jump to the cursor

    def _box_width(self) -> int:
        """Comments' boxes: the same width, all of them, as wide as the view leaves for one
        under the code (BOX_WIDTH at most)."""
        room = self.size.width - len(INDENT) - 2  # past the line numbers, a scrollbar
        return max(40, min(BOX_WIDTH, room)) if self.size.width else BOX_WIDTH

    def on_resize(self) -> None:
        """Wider or narrower (z, the terminal): the boxes laid out again, to fit."""
        if self._rows and self._box_width() != self._laid_out_at:
            self.show(self.comments)

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
        """Open or close the comment under the cursor, or the gap (lines left out), or else
        the file it's in."""
        row = self.current()
        if row and row.said:
            self.toggle_said(row.said)
        elif row and row.gap:
            self.toggle_gap(row.file, row.gap)
        elif row:
            self.toggle(row.file)

    def toggle_gap(self, file: int, gap: Gap) -> None:
        """Show a gap's lines, or hide them; the file's read first, the first time."""
        if self.files[file].path in self.sources:
            self.reveal(file, gap.at)
        else:
            self.post_message(self.NeedsFile(file, gap.at))

    def reveal(self, file: int, at: int) -> None:
        """Show the gap's lines, or hide them, its row staying where it is on the screen:
        lines above a hunk's header come in above it, those after the last hunk below."""
        where = self._gap_row(file, at)
        on_screen = (self.cursor if where is None else where) - round(self.scroll_offset.y)
        self.revealed ^= {(file, at)}
        self.show(self.comments)
        now = self._gap_row(file, at)
        if now is None:  # read, the file ends with the last hunk
            self.notify("That's the end of the file.")
            return
        self._place(now)
        # Once the view's taken its new height (until then, it can't scroll that far).
        self.call_after_refresh(self.scroll_to, y=max(now - on_screen, 0), animate=False)

    def _gap_row(self, file: int, at: int) -> int | None:
        """Where a gap's row is, if it has one."""
        found = (i for i, r in enumerate(self._rows) if r.gap and (r.file, r.gap.at) == (file, at))
        return next(found, None)

    def toggle_said(self, said: Said) -> None:
        self.shut ^= {said.key}
        self.show(self.comments)
        self._move_to(next(i for i, r in enumerate(self._rows) if r.said == said))

    def toggle_all(self) -> None:
        everything = set(range(len(self.files)))
        self.show(self.comments, set() if self.opened == everything else everything)

    # -- Moving and selecting ------------------------------------------------------

    def next_change(self, step: int) -> None:
        """The cursor to the next (or previous) change: a file's line or, in an opened file,
        a hunk's ``@@`` line; brought near the top, with what follows it in view."""
        starts = [i for i, row in enumerate(self._rows) if _starts_a_change(row)]
        later = [i for i in starts if (i - self.cursor) * step > 0]
        if not later:
            return
        self._go(min(later) if step > 0 else max(later), glide=True)
        top, height = round(going_to(self)), self.size.height
        if not top <= self.cursor < top + height // 2:  # not in the view's upper half
            glide_to(self, self.cursor - 1)

    def action_move(self, step: int) -> None:
        """Up or down a row; a comment's box is one, however many lines it takes."""
        first, last = self._unit(self.cursor)
        self._go(last + step if step > 0 else first + step, glide=True)

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
        self._go(self.cursor + step * max(self.size.height - 1, 1), glide=True)

    def action_select(self, step: int) -> None:
        """Select up or down a line of code (over comments), in the file the cursor's in."""
        if self.anchor is None:
            self.anchor = self.cursor
        self._move_to(self._next_line(step), glide=True)

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

    def to_file(self, path: str) -> None:
        """The cursor on a file's line, in view, if the diff has it."""
        index = next((i for i, file in enumerate(self.files) if file.path == path), None)
        if index is not None:
            self._go(self._file_row(index))

    def _go(self, index: int, *, glide: bool = False) -> None:
        """Move, letting a selection go, unless it's from a mark."""
        if not self.marking:
            self.anchor = None
        self._move_to(index, glide=glide)

    def _move_to(self, index: int, *, glide: bool = False) -> None:
        """The cursor to a row (a box's first, if it's in one), and all of it into view:
        gliding there, for keys that move it (see purr.tui.scrolling)."""
        self._place(index)
        self._into_view(*self._here, glide=glide)

    def _place(self, index: int) -> None:
        """The cursor on a row (a box's first, if it's in one), wherever the view is."""
        index = min(max(index, 0), max(len(self._rows) - 1, 0))
        self._here = self._unit(index)
        self.cursor = self._here[0]
        self.refresh()
        on_comment = bool(self._rows and self._rows[self.cursor].said)
        if on_comment != self._on_comment:  # r: reply on a comment, review elsewhere
            self._on_comment = on_comment
            self.refresh_bindings()

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

    def _into_view(self, first: int, last: int, *, glide: bool = False) -> None:
        top, height = going_to(self), self.size.height
        if first < top:
            y = first
        elif height and last >= top + height:
            y = min(first, last - height + 1)
        else:
            return
        glide_to(self, y) if glide else self.scroll_to(y=y, animate=False)

    def outside_selected(self) -> bool:
        """Whether what's selected (or the line under the cursor) has lines from a gap,
        which GitHub takes no comments on."""
        return any(line.outside for line in self.selected())

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
        opens or shuts the file, and on a gap's, its lines."""
        offset = event.get_content_offset(self)
        index = self.scroll_offset.y + offset.y if offset else len(self._rows)
        if index >= len(self._rows):
            return
        if event.shift and self.anchor is None:
            self.anchor = self.cursor
        (self._move_to if event.shift else self._go)(index)
        row = self._rows[index]
        if row.kind == "file" and not event.shift:
            self.toggle(row.file)
        elif row.gap and not event.shift:
            self.toggle_gap(row.file, row.gap)

    # -- Drawing ---------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        index = self.scroll_offset.y + y
        width = self.size.width
        if index >= len(self._rows):
            return Strip.blank(width, self.rich_style)
        # The view's colors under the line's own, green or red behind a line added or
        # removed; the cursor's, or the selection's, across the whole row.
        style = self.rich_style + _changed(self._rows[index]) + self._highlight(index)
        x = self.scroll_offset.x
        return self._strip(index).apply_style(style).crop_extend(x, x + width, style)

    def _highlight(self, index: int) -> Style:
        first, last = self._here  # the cursor's row, or its box
        if first <= index <= last:
            return self._background("diff-view--cursor")
        if self.anchor is not None and min(self.anchor, self.cursor) <= index <= max(
            self.anchor, self.cursor
        ):  # what selected() takes: from where you started to the cursor, both in
            return self._background("diff-view--selected")
        return Style()

    def _strip(self, index: int) -> Strip:
        if index not in self._strips:
            text = self._rows[index].text
            self._strips[index] = Strip(list(text.render(self.app.console, end="")))
        return self._strips[index]

    def _background(self, component: str) -> Style:
        return Style(bgcolor=self.get_component_rich_style(component).bgcolor)


def _starts_a_change(row: Row) -> bool:
    """A file's line, or a hunk's ``@@`` (lines left out above it, or not): where n and p
    go."""
    return row.kind == "file" or (row.line is not None and row.line.kind == "@@")


def _changed(row: Row) -> Style:
    """Green behind a line the diff adds, red behind one it removes (the theme's)."""
    if row.kind != "line" or row.line is None or row.line.kind not in ("+", "-"):
        return Style()
    colors = palette()
    return Style(bgcolor=colors.added if row.line.kind == "+" else colors.removed)


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


_CHANGES = Binding.Group("Changes", compact=True)  # files, and an opened file's hunks


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
        Binding("r", "reply", "Reply"),  # on a comment on GitHub; elsewhere, the repo's r
        Binding("x", "remove", "Delete"),
        Binding("e", "toggle_all", "All"),
        Binding("n", "next_change(1)", "Next change", group=_CHANGES),
        Binding("p", "next_change(-1)", "Previous change", group=_CHANGES),
        Binding("escape", "repo('unzoom')", "Zoom out"),  # zoomed: esc zooms out first
        Binding("escape,d", "close", "Close"),  # selecting, esc unselects: d, then
        *pane_keys(["zoom", "approve", "merge"]),
        Binding("shift+tab", "repo('switch_pane')", "Review", key_display="⇧tab"),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pr: PR | None = None
        self._diff = PRDiff(diff="")
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

    async def show(self, pr: PR, diff: PRDiff) -> None:
        """The PR's diff at its head (with what the agent committed that's not pushed yet),
        with what's been said on it: first, on the PR itself (the conversation), then file
        by file."""
        self.pr, self._diff, self._threads = pr, diff, []
        self._files = (CONVERSATION, *parse(diff.diff))
        self.border_title = _diff_title(pr, diff)
        self.view.load(self._files)
        comments = await self._comments()
        self.view.show(comments, first_opened(self._files, diff.diff, comments))
        self._read_threads()

    async def update(self, pr: PR, diff: PRDiff) -> None:
        """The PR's diff again, if it changed (the agent committed, or pushed): the same
        files open, the cursor on the file it was in."""
        if self.pr is None or pr.ref != self.pr.ref or diff == self._diff:
            return
        self.pr, self._diff = pr, diff
        comments = await self._comments()  # first: what follows happens at once
        if self._diff is not diff:
            return  # another came meanwhile
        view = self.view
        opened = {file.path for i, file in enumerate(view.files) if i in view.opened}
        here = view.current()
        path = view.files[here.file].path if here else ""
        self._files = (CONVERSATION, *parse(diff.diff))
        self.border_title = _diff_title(pr, diff)
        view.load(self._files)
        view.show(comments, {i for i, file in enumerate(self._files) if file.path in opened})
        view.to_file(path)

    @on(DiffView.NeedsFile)
    def _needs_file(self, event: DiffView.NeedsFile) -> None:
        self.run_worker(self._read_file(event.file, event.at), group="file", exclusive=True)

    async def _read_file(self, file: int, at: int) -> None:
        """The file as it is in the PR (from Purr's clone: a moment), then the gap's lines."""
        pr, files = self.pr, self.view.files
        if pr is None:
            return
        path = files[file].path
        try:
            request = GetFile(pr=pr.ref, path=path, commit=self._diff.unpushed)
            text = (await self.purr_app.bus.request(request)).text
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        if self.pr is pr and self.view.files is files:  # still the diff it was asked for
            self.view.sources[path] = text
            self.view.reveal(file, at)

    async def reload(self) -> None:
        """What's been said changed (yours, the agent's proposal)."""
        if self.pr:
            self.view.show(await self._comments())

    def _read_threads(self) -> None:
        """The PR's review threads on GitHub, read (again) in the background."""
        self.run_worker(self._load_threads(), group="threads", exclusive=True)

    async def _load_threads(self) -> None:
        """The PR's review threads on GitHub (a request away): their files open as they come,
        unless all of them are resolved."""
        assert self.pr is not None
        ref = self.pr.ref
        try:
            reply = await self.purr_app.bus.request(GetReviewThreads(pr=ref))
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        if not self._still(ref):
            return  # another PR's diff now: these aren't its threads
        self._threads = reply.threads
        comments = await self._comments()
        if not self._still(ref):
            return
        open_on = {c.path for c in comments if c.source == "github" and not c.resolved}
        on = {i for i, file in enumerate(self._files) if file.path in open_on}
        self.view.show(comments, self.view.opened | on)

    def _still(self, ref: PRRef) -> bool:
        """Whether it's still this PR's diff: an answer can come after you've moved on."""
        return self.pr is not None and self.pr.ref == ref

    async def _comments(self) -> list[Said]:
        """Yours on this commit, those the agent proposes posting, the PR's threads, and
        what's said on the PR itself (with its details)."""
        assert self.pr is not None
        store = self.purr_app.purr.store
        head = self.pr.head_sha
        mine = [c for c in await store.draft_comments(self.pr.ref) if c.head_sha == head]
        actions = await store.pending_actions(self.pr.ref)
        proposed = actions[0].payload.get("comments") or [] if actions else []
        at = actions[0].created_at if actions else ""
        details = self.purr_app.details.get(self.pr)
        conversation = details.conversation if details else ()
        found = said(mine, proposed, self._threads, at, conversation)
        return moved(found, self._diff.moved) if self._diff.moved else found

    async def action_repo(self, action: str) -> None:
        await self.purr_app.main.repo.run_action(action)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if parameters == ("unzoom",):
            return self.purr_app.main.repo.zoomed
        if parameters == ("approve",):
            return bool(self.purr_app.main.repo.a_does())
        if action == "reply":  # on a comment on GitHub; elsewhere r reviews (the repo's)
            row = self.view.current()
            return bool(row and row.said and row.said.source == "github")
        return True

    def action_reply(self) -> None:
        """r on a comment on GitHub: your answer, starting with its words quoted (as
        GitHub's quote reply does), posted when you save it: in its thread, or on the PR."""
        row = self.view.current()
        if not (self.pr and row and row.said):
            return
        said = row.said
        quote = quoted(said.text)
        if said.path:
            heading = f"Your reply on GitHub, to {said.who} on {said.path}:{said.line}"
            self._write(CommentScreen(heading, quote), partial(self._reply, said))
        else:
            heading = f"Your comment on the PR, to {said.who}"
            self._write(CommentScreen(heading, quote), partial(self._on_pr, said))

    def action_close(self) -> None:
        self.post_message(self.Closed())

    def action_toggle_all(self) -> None:
        self.view.toggle_all()

    def action_next_change(self, step: int) -> None:
        self.view.next_change(step)

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
        """The editor, on what's said here (see _editing): yours, rewritten; the agent's
        proposed comment, rewritten (approving posts yours); one on GitHub, changed there
        if it's yours, else answered (in its thread, or on the PR) when you save it."""
        if said.key in self.view.shut:
            self.view.toggle_said(said)
            return
        heading, body, save = self._editing(said)
        self._write(CommentScreen(heading, body), partial(save, said))

    def _editing(self, said: Said) -> tuple[str, str, Callable[[Said, str], Awaitable[None]]]:
        """What the editor says, what it starts with, and what saving it does."""
        if not said.path:  # on the PR itself
            if said.kind == "comment" and said.yours:
                heading = "Your comment on the PR (saving changes it there)"
                return heading, said.text, self._edit_pr_comment
            return f"Your comment on the PR, to {said.who}", f"@{said.who} ", self._on_pr
        where = f"{said.path}:{said.line}"
        if said.source == "you":
            return f"Your comment on {where}", said.text, self._rewrite_mine
        if said.source == "agent":
            return f"The agent's proposed comment on {where}", said.text, self._rewrite_proposed
        if said.yours and said.id:
            heading = f"Your comment on GitHub, on {where} (saving changes it there)"
            return heading, said.text, self._edit_on_github
        return f"Your reply on GitHub, to {said.who} on {where}", "", self._reply

    async def _rewrite_mine(self, said: Said, text: str) -> None:
        """One of yours from before comments went as you saved them, rewritten."""
        assert self.pr is not None and said.mine is not None
        mine = said.mine
        job = SaveComment(pr=self.pr.ref, path=mine.path, line=mine.line, side=mine.side,
                          body=text, head_sha=self.pr.head_sha, comment_id=mine.id)  # fmt: skip
        await self._request(job)

    async def _rewrite_proposed(self, said: Said, text: str) -> None:
        assert self.pr is not None
        index = int(said.key.partition(":")[2])
        await self._request(EditProposedComment(pr=self.pr.ref, index=index, body=text))

    async def _edit_on_github(self, said: Said, text: str) -> None:
        assert self.pr is not None
        await self._request(EditGitHubComment(pr=self.pr.ref, comment_id=said.id, body=text))
        self.notify("Changed on GitHub")
        self._read_threads()

    async def _edit_pr_comment(self, said: Said, text: str) -> None:
        assert self.pr is not None
        if await self._request(EditPRComment(pr=self.pr.ref, comment_id=said.id, body=text)):
            self.notify("Changed on GitHub")
        await self._reread_conversation()

    async def _on_pr(self, said: Said | None, text: str) -> None:
        """Your comment on the PR itself, posted now."""
        assert self.pr is not None
        if await self._request(PostPRComment(pr=self.pr.ref, body=text)):
            self.notify("Posted on GitHub")
        await self._reread_conversation()

    async def _reread_conversation(self) -> None:
        """What's said on the PR, read again (with its details), and shown, opened."""
        assert self.pr is not None
        await self.purr_app.main.repo.refresh_details(self.pr)
        self.view.show(await self._comments(), self.view.opened | {0})

    async def _reply(self, said: Said, text: str) -> None:
        assert self.pr is not None
        await self._request(ReplyToThread(pr=self.pr.ref, thread_id=said.thread, body=text))
        self.notify("Replied on GitHub")
        self._read_threads()

    def action_comment(self) -> None:
        """On the conversation, a comment on the PR; else one on the lines selected (or the
        one under the cursor, or someone's comment's): posted on GitHub when you save it.
        On a comment of yours not posted yet (from before), it's rewritten."""
        row = self.view.current()
        said = row.said if row else None
        if row and not self.view.files[row.file].path:
            self._write(CommentScreen("Your comment on the PR (posted when you save it)"),
                        partial(self._on_pr, None))  # fmt: skip
        elif why := self._no_comments_here(said):
            self.notify(why)
        elif said and said.mine:
            screen = CommentScreen(f"Your comment on {_mine_text(said.mine)}", said.mine.body)
            self._write(screen, partial(self._rewrite_mine, said))
        elif quote := self._quoted(row):
            heading = f"Your comment on {_where_text(quote)} (posted when you save it)"
            self._write(CommentScreen(heading), partial(self._post_on_lines, quote))
        else:
            self.notify("Comments go on lines of the diff (or the conversation, at the top).")

    def _no_comments_here(self, said: Said | None) -> str:
        """Why GitHub won't take a comment on what's selected, if it won't."""
        if self._diff.unpushed and not (said and said.mine):
            return (
                "This diff has commits that aren't pushed yet, and GitHub takes comments only "
                "on lines it has. Push them first (a), or ask the agent about these (enter)."
            )
        if self.view.outside_selected():
            return (
                "GitHub takes comments only on the diff's lines, not those shown around "
                "them. enter asks the agent about them."
            )
        return ""

    def _write(self, screen: "CommentScreen", save: Callable[[str], Awaitable[None]]) -> None:
        """The comment editor; what saving it does, with what was written."""

        async def saved(body: str | None) -> None:
            if body:
                await save(body)

        self.app.push_screen(screen, saved)

    async def _post_on_lines(self, quote: Quote, body: str) -> None:
        assert self.pr is not None
        job = PostLineComment(
            pr=self.pr.ref, path=quote.path, line=quote.end, side=quote.side,
            start_line=quote.start, start_side=quote.start_side, body=body,
            head_sha=self.pr.head_sha,
        )  # fmt: skip
        if await self._request(job):
            self.notify("Posted on GitHub")
        self._read_threads()

    def _quoted(self, row: Row | None) -> Quote | None:
        """What a comment would be on: the selection, or the line of the comment under the
        cursor."""
        if row and row.said and row.said.line and self.view.anchor is None:
            line, side = row.said.line, row.said.side
            return Quote(row.said.path, line, side, line, side, "")
        return self.view.quote()

    async def action_remove(self) -> None:
        """x on a comment: yours on GitHub deleted there (once you've said so), one the
        agent proposes left out of its proposal, one of yours not posted yet dropped."""
        row = self.view.current()
        said = row.said if row else None
        if not (self.pr and said):
            self.notify("x deletes a comment of yours: move to one first.")
        elif said.mine:
            await self._request(RemoveComment(pr=self.pr.ref, comment_id=said.mine.id))
        elif said.source == "agent":
            index = int(said.key.partition(":")[2])
            await self._request(DropProposedComment(pr=self.pr.ref, index=index))
        elif said.yours and said.id and said.kind in ("thread", "comment"):
            self._delete(said)
        else:
            self.notify("x deletes a comment of yours (or one the agent proposes).")

    def _delete(self, said: Said) -> None:
        """Your comment on GitHub, deleted there, once you've said so."""
        assert self.pr is not None
        job = DeleteGitHubComment(pr=self.pr.ref, comment_id=said.id, on_line=bool(said.path))

        async def answered(yes: bool | None) -> None:
            if not yes:
                return
            if await self._request(job):
                self.notify("Deleted on GitHub")
            if said.path:
                self._read_threads()
            else:
                await self._reread_conversation()

        question = ConfirmScreen("Delete your comment on GitHub?", "It can't be brought back.")
        self.app.push_screen(question, answered)

    async def _request(self, job: Request) -> bool:
        """Whether it was done (if not, saying why)."""
        try:
            await self.purr_app.bus.request(job)
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return False
        finally:
            await self.reload()
        return True


def quoted(text: str) -> str:
    """``> `` before each of its lines, as a reply quoting it starts; then room to answer."""
    lines = text.strip().splitlines() or [""]
    return "\n".join(f"> {line}".rstrip() for line in lines) + "\n\n"


def _where_text(quote: Quote) -> str:
    """``src/bus.py:404–412`` (``, deleted`` if it's on deleted lines)."""
    lines = f"{quote.start}–{quote.end}" if quote.start != quote.end else str(quote.end)
    deleted = ", deleted" if quote.side == "LEFT" and quote.start_side == "LEFT" else ""
    return f"{quote.path}:{lines}{deleted}"


def _mine_text(mine: DraftComment) -> str:
    first = mine.start_line or mine.line
    lines = f"{first}–{mine.line}" if first != mine.line else str(mine.line)
    return f"{mine.path}:{lines}"
