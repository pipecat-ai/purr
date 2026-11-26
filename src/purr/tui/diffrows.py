"""The diff view's rows: a line for each file, then, if it's opened, its lines, numbered
and in their language's colors, a row for each stretch the diff leaves out around its
hunks, and what's said on each line, in a box (see purr.tui.boxes).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple

from rich.text import Text

from purr.diff import File, Gap, Line, gaps, mover, parse
from purr.models import DraftComment, PRComment
from purr.tui.boxes import BOX_WIDTH, comment_box
from purr.tui.code import code, colored_source, highlighted
from purr.tui.overview import VERDICTS, short_age
from purr.tui.theme import palette

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


# How a diff's lines start, colored as git does: a hunk's header, its +, - and git's notes.
LINE_STYLES = {"@@": "cyan", "+": "bold green", "-": "bold red", "\\": "dim"}


# What's said on the PR itself, not on a line: the first "file" of the view, its comments
# boxes like those on lines.
CONVERSATION = File("", ())


@dataclass(frozen=True)
class Said:
    """A comment, a box of its own: on a line, yours not posted yet (``mine``, from before
    comments went out as you saved them), one the agent proposes, or one in a review thread
    on GitHub (``thread``, to reply in; ``reply`` if it isn't the thread's first); or on
    the PR itself (no ``path``): a comment, or a review's text. ``yours`` if you wrote it,
    to change it or delete it there; ``kind`` says which GitHub has it as."""

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
    kind: str = ""  # on GitHub: "thread" (on lines), "comment" or "review" (on the PR)
    note: str = ""  # a review's state: "approved", ...


@dataclass(frozen=True)
class Row:
    """A line of the view: a file's, one of its diff lines, a comment's, or a note."""

    kind: str  # "file", "line", "said", "note", "gap"
    text: Text
    file: int  # which file it's in
    line: Line | None = None
    said: Said | None = None
    gap: Gap | None = None  # lines left out, which tab shows (on a hunk's header, say)

    def key(self) -> tuple:
        """What it shows, to find it again once the rows change (a comment by its key:
        rewriting it doesn't make it another)."""
        said = self.said.key if self.said else None
        return (self.kind, self.file, self.line, said, self.gap.at if self.gap else None)


class _Around(NamedTuple):
    """A file's gaps, as shown: those opened (by where they are), and the file as it is in
    the PR, once read (None until then)."""

    opened: frozenset[int] = frozenset()
    source: str | None = None


def said(
    mine: Sequence[DraftComment],
    proposed: Sequence[dict],
    threads: Sequence[dict] = (),
    proposed_at: str = "",
    conversation: Sequence[PRComment] = (),
) -> list[Said]:
    """Your comments, the agent's proposed ones (``proposed_at``), the PR's review threads
    on GitHub, and what's said on the PR itself."""
    yours = [_yours(comment) for comment in mine]
    theirs = [
        Said(str(c["path"]), int(c["line"]), "RIGHT", "agent", str(c.get("body") or ""),
             "agent", f"agent:{i}", at=proposed_at)
        for i, c in enumerate(proposed)
        if c.get("path") and c.get("line")
    ]  # fmt: skip
    on_github = [said for thread in threads if thread.get("path") for said in _thread(thread)]
    return yours + theirs + on_github + [_on_pr(comment) for comment in conversation]


def moved(comments: Sequence[Said], changes: str) -> list[Said]:
    """What's said on lines of the PR as on GitHub, on those lines where they are after the
    commits not pushed yet (``changes``, a diff with no context): on none (outdated) if the
    commits changed them. Those on the base's lines (LEFT) stay put."""
    where = mover(changes)

    def move(said: Said) -> Said:
        if not said.path or said.side != "RIGHT" or not said.line:
            return said
        line = where(said.path, said.line)
        start = where(said.path, said.start) if said.start and line else 0
        return replace(said, line=line, start=start or None)

    return [move(said) for said in comments]


def _on_pr(comment: PRComment) -> Said:
    """A comment on the PR itself, or a review's text."""
    return Said("", 0, "", comment.author, comment.body, "github", f"pr:{comment.id}",
                yours=comment.mine, id=comment.id, at=comment.at, kind=comment.kind,
                note=VERDICTS.get(comment.state, ""))  # fmt: skip


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
             id=str(comment.get("id") or ""), at=str(comment.get("at") or ""), kind="thread")
        for i, comment in enumerate(thread["comments"])
    ]  # fmt: skip


def rows(
    files: Sequence[File],
    opened: set[int],
    comments: Sequence[Said],
    shut: set[str] = frozenset(),
    width: int = BOX_WIDTH,
    *,
    revealed: set[tuple[int, int]] = frozenset(),
    sources: Mapping[str, str] | None = None,
) -> list[Row]:
    """The view's lines: each file's, then, if it's opened, its lines and their comments
    (those in ``shut`` as a line each; the others in boxes ``width`` wide), and a row for
    each gap around its hunks, with its lines if it's in ``revealed`` (as (file, where the
    gap is)), from ``sources`` (the files as they are in the PR, by path)."""
    out: list[Row] = []
    for index, file in enumerate(files):
        on_file = [c for c in comments if c.path == file.path]
        out.append(Row("file", _file_text(file, index in opened, on_file), index))
        if index in opened:
            shown = frozenset(at for i, at in revealed if i == index)
            around = _Around(shown, (sources or {}).get(file.path))
            out += _file_rows(index, file, on_file, shut, width, around)
    return out


def _file_text(file: File, opened: bool, comments: list[Said]) -> Text:
    if not file.path:  # the conversation
        text = Text.assemble(("▾ " if opened else "▸ ", "dim"), ("Conversation", "bold"))
        text.append("  on the PR" if comments else "  nothing said on the PR yet", style="dim")
        text.append("  · c writes on it", style="dim")
    else:
        text = file_line(file, opened)
    if comments:
        text.append(f"  ✎ {len(comments)}", style="yellow")
    return text


def _file_rows(
    index: int, file: File, comments: list[Said], shut: set[str], width: int, around: _Around
) -> list[Row]:
    if not file.path:  # the conversation: its comments, oldest first
        return [row for comment in comments for row in _said_rows(index, comment, shut, width)]
    if file.generated:
        return [Row("note", Text("  generated, not shown", style="dim"), index)]
    out: list[Row] = []
    colored = highlighted(file, palette().code)
    left_out = {gap.at: gap for gap in gaps(file)}
    for at, (line, words) in enumerate(zip(file.lines, colored, strict=True)):
        if gap := left_out.get(at):  # its header: the gap's row, its lines above it
            out += _gap_rows(index, file, gap, line, around)
        else:
            out.append(Row("line", _numbered(line, words), index, line))
        for comment in (c for c in comments if (c.line, c.side) == line.target()):
            out += _said_rows(index, comment, shut, width)
    if end := left_out.get(len(file.lines)):  # after the last hunk: its row, then its lines
        out += _gap_rows(index, file, end, None, around)
    placed = {line.target() for line in file.lines}
    for comment in (c for c in comments if (c.line, c.side) not in placed):
        out += _said_rows(index, comment, shut, width, where=True)
    return out


def _gap_rows(index: int, file: File, gap: Gap, header: Line | None, around: _Around) -> list[Row]:
    """A gap's row, and its lines if it's opened: above a hunk's header, or below the row
    after the last hunk. Once the file's read, nothing after a last hunk that ends it."""
    source = around.source.splitlines() if around.source is not None else None
    lines = gap.lines(source) if source is not None else []
    if source is not None and not lines:
        return []  # the last hunk is the end of the file
    opened = gap.at in around.opened and bool(lines)
    row = Row("gap", _gap_text(gap, header, opened, len(lines) or None), index, header, gap=gap)
    if not opened:
        return [row]
    words = colored_source(file.path, around.source or "", palette().code)
    shown = [Row("line", _numbered(line, words[line.new - 1]), index, line) for line in lines]
    return [*shown, row] if header else [row, *shown]


def _gap_text(gap: Gap, header: Line | None, opened: bool, size: int | None) -> Text:
    """``⋯ 37 lines (tab)  @@ -398,21 +398,30 @@ class Service:``, what's left out first
    (a long header runs off a narrow pane); after the last hunk, ``⋯ 12 more lines
    (tab)``, or the rest of the file, until it's read. Shown: ``▴``, ``▾``."""
    text = Text(" " * len(INDENT))
    size = size or (gap.last - gap.first + 1 if gap.last else None)
    if not size:
        text.append("⋯ the rest of the file (tab)", "dim")
    else:
        mark = ("▴" if header else "▾") if opened else "⋯"
        more = "" if header else "more "
        text.append(f"{mark} {size} {more}line{'s' if size != 1 else ''} (tab)", "dim")
    if header:
        text.append("  ").append(header.text.expandtabs(4), LINE_STYLES["@@"])
    return text


def _numbered(line: Line, words: Text | None = None) -> Text:
    """A diff line with its numbers (in the base, and in the PR's version): its +, - or
    space, then its code, in ``words``' colors if it has them (its language's). A hunk's
    header and git's notes, as git colors them. (The green or red behind a line is the
    view's: see DiffView.render_line.)"""
    gutter = Text(f"{line.old or '':>5}{line.new or '':>5}  ", style="dim")
    style = LINE_STYLES.get(line.kind, "")
    if line.kind not in ("+", "-", " "):
        return gutter.append(line.text.expandtabs(4), style)
    # The code bright, as a list's titles are: what's read here (the theme's own text
    # color reads dimmed on the green and red). What its language marks, colored.
    words = words.copy() if words is not None else Text(code(line))
    words.stylize_before(palette().name)
    return gutter.append(line.text[:1], style).append_text(words)


INDENT = " " * 12  # under the code, past the line numbers
_COLORS = {"you": "yellow", "agent": "magenta", "github": "cyan"}


def _said_rows(
    index: int, said: Said, shut: set[str], width: int, *, where: bool = False
) -> list[Row]:
    """A comment under its line: in a box, or (shut) a line saying who and how it starts."""
    texts = [_shut_line(said, where)] if said.key in shut else _box(said, where, width)
    return [Row("said", text, index, said=said) for text in texts]


def _title(said: Said, where: bool) -> str:
    """Who wrote it and when, and what else to know: ``alice · 3h ago · reply · resolved``,
    ``agent, proposed · now``, ``you · lines 4–9`` (and where, if it isn't on a line here)."""
    title = "agent, proposed" if said.source == "agent" else said.who
    title += " (you)" if said.yours else ""
    notes = [_when(said.at)] if said.at else []
    notes += [f"lines {said.start}–{said.line}"] if said.start else []
    notes += [said.note] if said.note else []
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


def _box(said: Said, where: bool, width: int) -> list[Text]:
    """The comment in a box (see purr.tui.boxes): its title (who wrote it, when) on the
    top border, all of it in its color (yours, the agent's, GitHub's), resolved or not."""
    title = f"▾ {_title(said, where)}"
    return comment_box(title, said.text, _COLORS[said.source], width, _indent(said))


def _indent(said: Said) -> str:
    """On a line, under the code (a reply too: its title says so); on the PR, none."""
    return INDENT if said.path else ""


def first_opened(files: Sequence[File], diff: str, comments: Sequence[Said]) -> set[int]:
    """The files opened at first: all of a short diff, else those with comments."""
    short = opens_short(diff)
    commented = {c.path for c in comments}
    return {i for i, file in enumerate(files) if file.path in short or file.path in commented}
