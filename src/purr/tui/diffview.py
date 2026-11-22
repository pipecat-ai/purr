"""The diff view (``d``): a PR's diff to read line by line, and comment on.

Files start as a line each (all of them opened if the diff is short; those with
comments on them, if not): ``enter`` opens or closes the one under the cursor,
``e`` all of them, ``n``/``p`` go to the next or previous file. ``c`` writes a
comment on the line under the cursor, or rewrites yours under it, and ``x``
removes it. Yours are posted with your next review of the PR (see
DraftComment); the comments the agent proposes show too.
"""

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Footer, Label, Static

from purr.diff import File, Line, parse
from purr.jobs import PurrError
from purr.messages import RemoveComment, SaveComment
from purr.models import PR, DraftComment
from purr.tui.overview import LINE_STYLES, file_line, opens_short
from purr.tui.prompt import PromptArea

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_WRAP = 100  # a comment's lines, at most this wide


@dataclass(frozen=True)
class Said:
    """A comment as the view shows it: yours (``mine`` set) or the agent's proposed."""

    path: str
    line: int
    side: str
    body: str
    mine: DraftComment | None = None


@dataclass(frozen=True)
class Row:
    """A line of the view: a file's, one of its diff lines, a comment's, or a note."""

    kind: str  # "file", "line", "said", "note"
    text: Text
    file: int  # which file it's in
    line: Line | None = None
    said: Said | None = None

    def key(self) -> tuple:
        """What it shows, to find it again once the rows change (a comment of yours by its
        id: rewriting it doesn't make it another)."""
        said = self.said.mine.id if self.said and self.said.mine else self.said
        return (self.kind, self.file, self.line, said)


def said(mine: Sequence[DraftComment], proposed: Sequence[dict]) -> list[Said]:
    """Your comments and the agent's proposed ones, as the view shows them."""
    yours = [Said(c.path, c.line, c.side, c.body, c) for c in mine]
    theirs = [
        Said(str(c["path"]), int(c["line"]), "RIGHT", str(c.get("body") or ""))
        for c in proposed
        if c.get("path") and c.get("line")
    ]
    return yours + theirs


def rows(files: Sequence[File], opened: set[int], comments: Sequence[Said]) -> list[Row]:
    """The view's lines: each file's, then, if it's opened, its lines and their comments."""
    out: list[Row] = []
    for index, file in enumerate(files):
        on_file = [c for c in comments if c.path == file.path]
        out.append(Row("file", _file_text(file, index in opened, on_file), index))
        if index in opened:
            out += _file_rows(index, file, on_file)
    return out


def _file_text(file: File, opened: bool, comments: list[Said]) -> Text:
    text = file_line(file, opened)
    if comments:
        text.append(f"  ✎ {len(comments)}", style="yellow")
    return text


def _file_rows(index: int, file: File, comments: list[Said]) -> list[Row]:
    if file.generated:
        return [Row("note", Text("  generated, not shown", style="dim"), index)]
    out: list[Row] = []
    for line in file.lines:
        out.append(Row("line", _numbered(line), index, line))
        for comment in (c for c in comments if (c.line, c.side) == line.target()):
            out += _said_rows(index, comment)
    placed = {line.target() for line in file.lines}
    for comment in (c for c in comments if (c.line, c.side) not in placed):
        out += _said_rows(index, comment, where=True)
    return out


def _numbered(line: Line) -> Text:
    """A diff line with its numbers: in the base, and in the PR's version."""
    gutter = f"{line.old or '':>5}{line.new or '':>5}  "
    return Text.assemble((gutter, "dim"), (line.text.expandtabs(4), LINE_STYLES.get(line.kind, "")))


def _said_rows(index: int, comment: Said, *, where: bool = False) -> list[Row]:
    """A comment under its line, wrapped: a row per line of it."""
    who, style = ("you", "yellow") if comment.mine else ("agent", "magenta")
    at = f" (line {comment.line}, not in the diff)" if where else ""
    first, *more = textwrap.wrap(comment.body, _WRAP) or [""]
    lead = f"{' ' * 12}✎ {who}{at}: "
    texts = [Text.assemble((lead, f"bold {style}"), (first, style))]
    texts += [Text(f"{' ' * len(lead)}{line}", style=style) for line in more]
    return [Row("said", text, index, said=comment) for text in texts]


def first_opened(files: Sequence[File], diff: str, comments: Sequence[Said]) -> set[int]:
    """The files opened at first: all of a short diff, else those with comments."""
    short = opens_short(diff)
    commented = {c.path for c in comments}
    return {i for i, file in enumerate(files) if file.path in short or file.path in commented}


class DiffView(ScrollView, can_focus=True):
    """The diff's lines, a cursor on one; long lines scroll sideways."""

    COMPONENT_CLASSES = {"diff-view--cursor"}
    DEFAULT_CSS = """
    DiffView { height: 1fr; }
    DiffView > .diff-view--cursor { background: $accent 30%; }
    """
    BINDINGS = [
        Binding("up,k", "move(-1)", "Up", show=False),
        Binding("down,j", "move(1)", "Down", show=False),
        Binding("pageup", "page(-1)", "Page up", show=False),
        Binding("pagedown", "page(1)", "Page down", show=False),
        Binding("home", "move(-1000000)", "Top", show=False),
        Binding("end", "move(1000000)", "Bottom", show=False),
    ]

    def __init__(self, files: Sequence[File], **kwargs):
        super().__init__(**kwargs)
        self.files = files
        self.opened: set[int] = set()
        self.comments: list[Said] = []
        self.cursor = 0
        self._rows: list[Row] = []
        self._strips: dict[int, Strip] = {}

    def show(self, comments: list[Said], opened: set[int] | None = None) -> None:
        """Lay the rows out again: other comments, other files opened. The cursor stays
        on the line it was on (or its file)."""
        here = self.current()
        self.comments = comments
        if opened is not None:
            self.opened = opened
        self._rows = rows(self.files, self.opened, comments)
        self._strips.clear()
        width = max((row.text.cell_len for row in self._rows), default=0)
        self.virtual_size = Size(width, len(self._rows))
        self._move_to(self._find(here))

    def current(self) -> Row | None:
        return self._rows[self.cursor] if 0 <= self.cursor < len(self._rows) else None

    def _find(self, row: Row | None) -> int:
        """Where a row (or, if it's gone, its file's) is now."""
        if row is None:
            return 0
        same = next((i for i, r in enumerate(self._rows) if r.key() == row.key()), None)
        return same if same is not None else self._file_row(row.file)

    def _file_row(self, file: int) -> int:
        return next((i for i, r in enumerate(self._rows) if r.file == file), 0)

    def toggle(self, file: int) -> None:
        self.show(self.comments, self.opened ^ {file})
        self._move_to(self._file_row(file))

    def toggle_all(self) -> None:
        everything = set(range(len(self.files)))
        self.show(self.comments, set() if self.opened == everything else everything)

    def next_file(self, step: int) -> None:
        """The cursor to the next (or previous) file's line."""
        starts = [i for i, row in enumerate(self._rows) if row.kind == "file"]
        later = [i for i in starts if (i - self.cursor) * step > 0]
        if later:
            self._move_to(min(later) if step > 0 else max(later))

    def action_move(self, step: int) -> None:
        self._move_to(self.cursor + step)

    def action_page(self, step: int) -> None:
        self._move_to(self.cursor + step * max(self.size.height - 1, 1))

    def _move_to(self, index: int) -> None:
        self.cursor = min(max(index, 0), max(len(self._rows) - 1, 0))
        top, height = self.scroll_offset.y, self.size.height
        if self.cursor < top:
            self.scroll_to(y=self.cursor, animate=False)
        elif height and self.cursor >= top + height:
            self.scroll_to(y=self.cursor - height + 1, animate=False)
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        offset = event.get_content_offset(self)
        index = self.scroll_offset.y + offset.y if offset else len(self._rows)
        if index < len(self._rows):
            self._move_to(index)
            if self._rows[index].kind == "file":
                self.toggle(self._rows[index].file)

    def render_line(self, y: int) -> Strip:
        index = self.scroll_offset.y + y
        width = self.size.width
        if index >= len(self._rows):
            return Strip.blank(width, self.rich_style)
        # The view's colors under the line's own, the cursor's across the whole row.
        style = self.rich_style + (self._cursor_style() if index == self.cursor else Style())
        x = self.scroll_offset.x
        return self._strip(index).apply_style(style).crop_extend(x, x + width, style)

    def _strip(self, index: int) -> Strip:
        if index not in self._strips:
            text = self._rows[index].text
            self._strips[index] = Strip(list(text.render(self.app.console, end="")))
        return self._strips[index]

    def _cursor_style(self) -> Style:
        return Style(bgcolor=self.get_component_rich_style("diff-view--cursor").bgcolor)


class CommentScreen(ModalScreen[str | None]):
    """Write a comment on a line: enter saves, ctrl+j starts a new line, esc cancels."""

    DEFAULT_CSS = """
    CommentScreen { align: center middle; }
    #comment { width: 84; }
    #comment-body { height: 6; margin: 1 0; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, where: str, body: str = ""):
        super().__init__()
        self._where = where
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="comment"):
            yield Label(f"Your comment on {self._where}", classes="hint")
            yield PromptArea(id="comment-body")
            yield Label("enter saves · ctrl+j new line · esc cancels", classes="hint")

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


class DiffScreen(ModalScreen[None]):
    """A PR's diff at its head, full screen (see the module's docstring)."""

    DEFAULT_CSS = """
    DiffScreen { background: $background; }
    #diff-title { height: 1; padding: 0 1; background: $panel; }
    """
    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("enter,space", "toggle", "Open/close"),
        Binding("e", "toggle_all", "All files"),
        Binding("n", "next_file(1)", "Next file"),
        Binding("p", "next_file(-1)", "Previous file"),
        Binding("c", "comment", "Comment"),
        Binding("x", "remove", "Remove yours"),
    ]

    def __init__(self, pr: PR, diff: str):
        super().__init__()
        self.pr = pr
        self._diff = diff
        self._files = parse(diff)

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def compose(self) -> ComposeResult:
        title = Text.assemble(
            (f"#{self.pr.number}", "bold"),
            f"  {self.pr.title}",
            ("   c on a line comments; yours go out with your next review (a)", "dim"),
        )
        yield Static(title, id="diff-title")
        yield DiffView(self._files, id="diff")
        yield Footer()

    async def on_mount(self) -> None:
        comments = await self._comments()
        self._view.show(comments, first_opened(self._files, self._diff, comments))
        self._view.focus()

    @property
    def _view(self) -> DiffView:
        return self.query_one(DiffView)

    async def _comments(self) -> list[Said]:
        """Yours on this commit, and those the agent proposes posting."""
        store = self.purr_app.purr.store
        mine = [c for c in await store.draft_comments(self.pr.ref) if c.head_sha == self._head]
        actions = await store.pending_actions(self.pr.ref)
        return said(mine, actions[0].payload.get("comments") or [] if actions else [])

    @property
    def _head(self) -> str:
        return self.pr.head_sha

    async def _reload(self) -> None:
        self._view.show(await self._comments())

    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle(self) -> None:
        if row := self._view.current():
            self._view.toggle(row.file)

    def action_toggle_all(self) -> None:
        self._view.toggle_all()

    def action_next_file(self, step: int) -> None:
        self._view.next_file(step)

    def action_comment(self) -> None:
        """A comment on the line under the cursor, or yours under it rewritten."""
        row = self._view.current()
        mine = row.said.mine if row and row.said else None
        target = row.line.target() if row and row.line else None
        spot = (mine.line, mine.side) if mine else target
        if row is None or spot is None:
            self.notify("Comments go on a line of the diff: move to one, then c.")
            return
        path = self._files[row.file].path
        line, side = spot
        where = f"{path}:{line}" + (" (deleted)" if side == "LEFT" else "")

        async def save(body: str | None) -> None:
            if body:
                await self._save(path, line, side, body, mine)

        self.app.push_screen(CommentScreen(where, mine.body if mine else ""), save)

    async def _save(
        self, path: str, line: int, side: str, body: str, mine: DraftComment | None
    ) -> None:
        job = SaveComment(
            pr=self.pr.ref,
            path=path,
            line=line,
            side=side,
            body=body,
            head_sha=self._head,
            comment_id=mine.id if mine else None,
        )
        await self._request(job)

    async def action_remove(self) -> None:
        row = self._view.current()
        if not (row and row.said and row.said.mine):
            self.notify("x removes a comment of yours: move to one first.")
            return
        await self._request(RemoveComment(pr=self.pr.ref, comment_id=row.said.mine.id))

    async def _request(self, job: SaveComment | RemoveComment) -> None:
        try:
            await self.purr_app.bus.request(job)
        except PurrError as e:
            self.notify(str(e), severity="warning")
        await self._reload()
