"""The diff view (``d``): a PR's diff to read line by line, and comment on.

Files start as a line each (all of them opened if the diff is short; those with
comments on them, if not): ``tab`` (or ``enter``) opens or closes the one the cursor's in,
``e`` all of them, ``n``/``p`` go to the next or previous file. ``c`` writes a
comment on the line under the cursor, or rewrites yours under it, and ``x``
removes it. Yours are posted with your next review of the PR (see
DraftComment). The comments the agent proposes show too, and the PR's review
threads on GitHub: each in a box under its line, which ``tab`` closes to a line
(resolved threads start that way) and opens again.
"""

import textwrap
from collections.abc import Sequence
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
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Footer, Label, Static

from purr.diff import File, Line, parse
from purr.jobs import PurrError
from purr.messages import GetReviewThreads, RemoveComment, SaveComment
from purr.models import PR, DraftComment
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
    """What was said on a line: a comment of yours (``mine``), one the agent proposes, or a
    review thread on GitHub. ``comments`` are each ``(who, what they said)``."""

    path: str
    line: int  # 0: on no line of this diff (outdated)
    side: str
    comments: tuple[tuple[str, str], ...]
    source: str  # "you", "agent", "github"
    key: str  # which it is, to open or close it
    resolved: bool = False
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
        """What it shows, to find it again once the rows change (a comment by its key:
        rewriting it doesn't make it another)."""
        return (self.kind, self.file, self.line, self.said.key if self.said else None)


def said(
    mine: Sequence[DraftComment], proposed: Sequence[dict], threads: Sequence[dict] = ()
) -> list[Said]:
    """Your comments, the agent's proposed ones, and the PR's review threads on GitHub."""
    yours = [
        Said(c.path, c.line, c.side, (("you", c.body),), "you", f"you:{c.id}", mine=c) for c in mine
    ]
    theirs = [
        Said(str(c["path"]), int(c["line"]), "RIGHT", (("agent", str(c.get("body") or "")),),
             "agent", f"agent:{i}")
        for i, c in enumerate(proposed)
        if c.get("path") and c.get("line")
    ]  # fmt: skip
    return yours + theirs + [_thread(thread) for thread in threads if thread.get("path")]


def _thread(thread: dict) -> Said:
    comments = tuple((c.get("author") or "?", c.get("body") or "") for c in thread["comments"])
    return Said(
        str(thread["path"]),
        int(thread.get("line") or 0),
        str(thread.get("side") or "RIGHT"),
        comments,
        "github",
        f"github:{thread.get('id')}",
        resolved=bool(thread.get("resolved")),
    )


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
    """``you``, ``agent, proposed``, ``alice and bob · resolved`` (and where, if not here)."""
    people = list(dict.fromkeys(who for who, _ in said.comments))
    title = {"you": "you", "agent": "agent, proposed"}.get(said.source) or " and ".join(people)
    notes = ["resolved"] if said.resolved else []
    notes += [_where(said)] if where else []
    return " · ".join([title, *notes])


def _where(said: Said) -> str:
    return f"line {said.line}, not in the diff" if said.line else "outdated"


def _shut_line(said: Said, where: bool) -> Text:
    who, first = said.comments[0] if said.comments else ("", "")
    more = f" (+{len(said.comments) - 1})" if len(said.comments) > 1 else ""
    style = "dim" if said.resolved else _COLORS[said.source]
    return Text(f"{_INDENT}▸ ✎ {_title(said, where)}: {_short(first)}{more}", style=style)


def _short(text: str, width: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _box(said: Said, where: bool) -> list[Text]:
    """The comment (each of a thread's, with who said it) in a box, wrapped."""
    many = len(said.comments) > 1
    body = [line for who, what in said.comments for line in _wrapped(who, what, many)]
    title = f"✎ {_title(said, where)}"
    width = max([cell_len(line) for line in body] + [cell_len(title) + 2])
    style = "dim" if said.resolved else _COLORS[said.source]
    top = f"{_INDENT}╭─ {title} {'─' * (width - cell_len(title) - 1)}╮"
    middle = [f"{_INDENT}│ {line}{' ' * (width - cell_len(line))} │" for line in body]
    bottom = f"{_INDENT}╰{'─' * (width + 2)}╯"
    return [Text(line, style=style) for line in [top, *middle, bottom]]


def _wrapped(who: str, what: str, many: bool) -> list[str]:
    """A comment's lines; in a thread of several, with who said it first."""
    lines = [
        line for paragraph in what.splitlines() for line in textwrap.wrap(paragraph, _WRAP) or [""]
    ] or [""]
    return [f"{who}: {lines[0]}", *[f"  {line}" for line in lines[1:]]] if many else lines


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
        Binding("up,k,ctrl+p", "move(-1)", "Up", show=False),
        Binding("down,j,ctrl+n", "move(1)", "Down", show=False),
        Binding("pageup", "page(-1)", "Page up", show=False),
        Binding("pagedown", "page(1)", "Page down", show=False),
        Binding("home", "move(-1000000)", "Top", show=False),
        Binding("end", "move(1000000)", "Bottom", show=False),
        # Here rather than on the screen: tab would move the focus otherwise.
        Binding("tab,enter,space", "toggle_here", "Open/close", key_display="tab"),
    ]

    def __init__(self, files: Sequence[File], **kwargs):
        super().__init__(**kwargs)
        self.files = files
        self.opened: set[int] = set()
        self.comments: list[Said] = []
        self.shut: set[str] = set()  # the comments shown as a line (resolved ones, at first)
        self._known: set[str] = set()  # the comments seen so far
        self.cursor = 0
        self._rows: list[Row] = []
        self._strips: dict[int, Strip] = {}

    def show(self, comments: list[Said], opened: set[int] | None = None) -> None:
        """Lay the rows out again: other comments, other files opened. The cursor stays
        on the line it was on (or its file)."""
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
            if self._rows[index].said:
                self.toggle_said(self._rows[index].said)
            elif self._rows[index].kind == "file":
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
        self._threads: list[dict] = []  # the PR's review threads on GitHub, once read

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
        self.run_worker(self._load_threads(), group="threads")

    async def _load_threads(self) -> None:
        """The PR's review threads on GitHub (a request away): their files open as they come,
        unless all of them are resolved."""
        try:
            reply = await self.purr_app.bus.request(GetReviewThreads(pr=self.pr.ref))
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        self._threads = reply.threads
        comments = await self._comments()
        open_on = {c.path for c in comments if c.source == "github" and not c.resolved}
        on = {i for i, file in enumerate(self._files) if file.path in open_on}
        self._view.show(comments, self._view.opened | on)

    @property
    def _view(self) -> DiffView:
        return self.query_one(DiffView)

    async def _comments(self) -> list[Said]:
        """Yours on this commit, those the agent proposes posting, and the PR's threads."""
        store = self.purr_app.purr.store
        mine = [c for c in await store.draft_comments(self.pr.ref) if c.head_sha == self._head]
        actions = await store.pending_actions(self.pr.ref)
        proposed = actions[0].payload.get("comments") or [] if actions else []
        return said(mine, proposed, self._threads)

    @property
    def _head(self) -> str:
        return self.pr.head_sha

    async def _reload(self) -> None:
        self._view.show(await self._comments())

    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle_all(self) -> None:
        self._view.toggle_all()

    def action_next_file(self, step: int) -> None:
        self._view.next_file(step)

    def action_comment(self) -> None:
        """A comment on the line under the cursor, or yours under it rewritten."""
        row = self._view.current()
        mine = row.said.mine if row and row.said else None
        target = row.line.target() if row and row.line else None
        if row and row.said and row.said.line:  # on someone's comment: one on the same line
            target = (row.said.line, row.said.side)
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
