"""A diff's code in its language's colors, line by line.

Each hunk is highlighted a side at a time, as the code reads before the PR and after
it (its context lines on both), so what spans lines (a string, a docstring, a comment)
is colored as a whole, as in an editor. The colors are the code's alone: the diff puts
its green and red behind them.
"""

from functools import lru_cache

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_for_filename
from pygments.util import ClassNotFound
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text

from purr.diff import File, Line

TAB = 4  # columns a tab takes, in the diff


def code(line: Line) -> str:
    """A diff line's code: without its +, - or space, tabs as spaces."""
    return line.text[1:].expandtabs(TAB)


@lru_cache(maxsize=64)
def highlighted(file: File, style: str) -> tuple[Text | None, ...]:
    """Each of the file's lines' code, colored in Pygments' ``style`` (None for what isn't
    code: hunk headers, git's notes, and every line of a file in no language it knows)."""
    lexer = _lexer(file.path)
    if lexer is None:
        return (None,) * len(file.lines)
    out: list[Text | None] = []
    hunk: list[Line] = []
    for line in file.lines:
        if line.kind in ("+", "-", " "):
            hunk.append(line)
            continue
        out += _hunk(hunk, lexer, style)
        hunk = []
        out.append(None)  # @@, or "\ No newline at end of file"
    return (*out, *_hunk(hunk, lexer, style))


@lru_cache(maxsize=16)
def colored_source(path: str, source: str, style: str) -> tuple[Text, ...]:
    """A whole file, a line each, colored as its language (plain if it has none): the
    lines a diff leaves out, when they're shown."""
    lines = source.splitlines()
    lexer = _lexer(path)
    if lexer is None:
        return tuple(Text(line.expandtabs(TAB)) for line in lines)
    whole = [Line(" ", f" {line}") for line in lines]
    return tuple(_colored(whole, lexer, style))


def _lexer(path: str) -> Lexer | None:
    try:
        # Leading and trailing blank lines kept, or the lines wouldn't line up.
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def _hunk(lines: list[Line], lexer: Lexer, style: str) -> list[Text]:
    """A hunk's lines colored: the code before (context and removed) and after (context
    and added), each as a whole, then each line from its side."""
    before = _colored([line for line in lines if line.kind != "+"], lexer, style)
    after = _colored([line for line in lines if line.kind != "-"], lexer, style)
    out = []
    for line in lines:
        if line.kind == "-":
            out.append(next(before))
        elif line.kind == "+":
            out.append(next(after))
        else:  # context: on both sides, the same
            next(before)
            out.append(next(after))
    return out


def _colored(lines: list[Line], lexer: Lexer, style: str):
    """The lines' code highlighted together, a line at a time, without the style's
    background."""
    if not lines:
        return iter(())
    source = "\n".join(code(line) for line in lines)
    text = Syntax(source, lexer, theme=style).highlight(source)
    return iter(_foreground(part) for part in text.split("\n", allow_blank=True)[: len(lines)])


def _foreground(text: Text) -> Text:
    """The text with its colors and emphasis only: no background, which is the diff's."""
    plain = Text(text.plain)
    for span in text.spans:
        style = span.style if isinstance(span.style, Style) else Style.parse(span.style)
        if style.bgcolor is not None:
            style = Style(
                color=style.color, bold=style.bold, italic=style.italic, underline=style.underline
            )
        plain.stylize(style, span.start, span.end)
    return plain
