"""A diff's code in its language's colors, line by line.

Each hunk is highlighted a side at a time, as the code reads before the PR and after
it (its context lines on both), so what spans lines (a string, a docstring, a comment)
is colored as a whole, as in an editor. The colors are the code's alone: the diff puts
its green and red behind them.

Only what a language marks (keywords, strings, numbers, built-ins, names being defined)
takes the style's colors. The rest (names, operators, punctuation) takes none, for the
diff to give it its own (bright: see diffrows._numbered), and comments are dim, as
Purr's other asides: a style's own text and operator colors read dimmed (Solarized's
operators are its comment color).
"""

from functools import lru_cache

from pygments.lexer import Lexer
from pygments.lexers import get_lexer_for_filename
from pygments.token import Comment, Error, Name, Operator, Punctuation, Text, Token, Whitespace
from pygments.token import _TokenType as TokenType
from pygments.util import ClassNotFound
from rich.style import Style
from rich.syntax import Syntax, SyntaxTheme
from rich.text import Text as RichText

from purr.diff import File, Line

TAB = 4  # columns a tab takes, in the diff
# What's in the regular text color: plain names, operators, punctuation, spaces.
_REGULAR = {Token, Text, Whitespace, Text.Whitespace, Name, Name.Other, Operator, Punctuation}
_REGULAR |= {Error}  # what the lexer didn't follow: as it's written


def code(line: Line) -> str:
    """A diff line's code: without its +, - or space, tabs as spaces."""
    return line.text[1:].expandtabs(TAB)


@lru_cache(maxsize=64)
def highlighted(file: File, style: str) -> tuple[RichText | None, ...]:
    """Each of the file's lines' code, colored in Pygments' ``style`` (None for what isn't
    code: hunk headers, git's notes, and every line of a file in no language it knows)."""
    lexer = _lexer(file.path)
    if lexer is None:
        return (None,) * len(file.lines)
    out: list[RichText | None] = []
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
def colored_source(path: str, source: str, style: str) -> tuple[RichText, ...]:
    """A whole file, a line each, colored as its language (plain if it has none): the
    lines a diff leaves out, when they're shown."""
    lines = source.splitlines()
    lexer = _lexer(path)
    if lexer is None:
        return tuple(RichText(line.expandtabs(TAB)) for line in lines)
    whole = [Line(" ", f" {line}") for line in lines]
    return tuple(_colored(whole, lexer, style))


def _lexer(path: str) -> Lexer | None:
    try:
        # Leading and trailing blank lines kept, or the lines wouldn't line up.
        return get_lexer_for_filename(path, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def _hunk(lines: list[Line], lexer: Lexer, style: str) -> list[RichText]:
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
    """The lines' code highlighted together, a line at a time."""
    if not lines:
        return iter(())
    theme = Syntax.get_theme(style)
    text = RichText()
    for kind, value in lexer.get_tokens("\n".join(code(line) for line in lines)):
        text.append(value, _style(kind, theme))
    return iter(text.split("\n", allow_blank=True)[: len(lines)])


def _style(kind: TokenType, theme: SyntaxTheme) -> Style | str:
    """A token's look: regular, dim (a comment), or the style's color for what it is,
    without the style's background (the diff's goes behind it)."""
    if kind in _REGULAR:
        return ""
    if kind in Comment:
        return "dim"
    marked = theme.get_style_for_token(kind)
    return Style(color=marked.color, bold=marked.bold, italic=marked.italic)
