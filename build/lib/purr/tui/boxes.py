"""Comments in boxes, as the diff and the PR's pane show them.

A box is a comment's words, rendered as Markdown, inside a rounded border in its color
(yours, the agent's, GitHub's), with who wrote it and when on the top border, and
@mentions picked out. Boxes in the same place are the same width.
"""

import io
import re
from functools import lru_cache

from rich.box import ROUNDED
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from purr.tui.theme import palette

BOX_WIDTH = 100  # a box, at most this wide
_MENTION = re.compile(r"(?<![\w/])@[A-Za-z0-9][\w-]*(?:/[\w.-]+)?")  # @alice, @org/team
# What boxes are drawn with: any console does, for their lines and styles.
_CONSOLE = Console(
    width=BOX_WIDTH, file=io.StringIO(), force_terminal=True, color_system="truecolor"
)


def comment_box(title: str, body: str, color: str, width: int, indent: str = "") -> list[Text]:
    """A comment's box, line by line: ``width`` wide, each line after ``indent``."""
    return list(_box(title, body, color, width, indent, palette()))


@lru_cache(maxsize=1024)
def _box(title: str, body: str, color: str, width: int, indent: str, colors) -> tuple[Text, ...]:
    """Drawn once for each comment and width (and theme: ``colors``, the palette)."""
    words = Markdown(body or " ", code_theme=colors.code)
    panel = Panel(
        Padding(words, (0, 1), style=f"on {colors.box}"),  # its inside on the box's background
        title=Text(title, style=f"bold {color}"),
        title_align="left",
        border_style=color,
        box=ROUNDED,
        padding=0,
        width=width,
    )
    top, *inside, bottom = (
        _line(indent, line)
        for line in _CONSOLE.render_lines(panel, _CONSOLE.options.update_width(width))
    )
    return (top, *_trimmed(inside), bottom)


def _trimmed(inside: list[Text]) -> list[Text]:
    """A box's inside without the blank lines Markdown leaves at its ends (before a list)."""
    words = [i for i, line in enumerate(inside) if line.plain.strip(" │")]
    return inside[words[0] : words[-1] + 1] if words else inside[:1]


def _line(indent: str, segments) -> Text:
    text = Text(indent)
    for segment in segments:
        if not segment.control:
            text.append(segment.text, segment.style)
    text.highlight_regex(_MENTION, "bold blue")
    return text


class CommentBox:
    """A comment's box, as wide as there's room for (up to BOX_WIDTH): for the PR's pane."""

    def __init__(self, title: str, body: str, color: str):
        self.title, self.body, self.color = title, body, color

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from comment_box(self.title, self.body, self.color, min(options.max_width, BOX_WIDTH))
