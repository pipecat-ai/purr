"""One-line rows for Purr's lists, quick to lay out.

A list (Textual's OptionList) works out how tall each row is before drawing any. For a
Rich renderable (a Table, a Rule) that means drawing it: every row, every time the list
is built or changes width (pipecat's PR list has 750 rows: half a second). These say
they're one line high, and are drawn only when shown, at the list's width.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text
from textual.content import Content
from textual.css.styles import RulesMap
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, Visual


@dataclass(frozen=True)
class Column:
    """A row's column: ``width`` cells (None: what the others leave), then ``gap`` spaces."""

    width: int | None
    gap: int
    justify: Literal["left", "right"] = "left"


class OneLine(Visual):
    """One line high, as wide as the list: what :meth:`line` draws at its width. Rich
    prints it too, as wide as it's given."""

    def line(self, width: int) -> Text:
        raise NotImplementedError

    @cached_property
    def looks(self) -> tuple:
        """What it shows, to tell whether it looks the same as another (Text's own == leaves
        out its style). Worked out once: a row doesn't change."""
        raise NotImplementedError

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.line(options.max_width)

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return container_width

    def get_height(self, rules: RulesMap, width: int) -> int:
        return 1

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        content = Content.from_rich_text(self.line(width))
        return content.render_strips(width, height, style, options)


class Row(OneLine):
    """Cells in columns, each cut to fit (with …) and padded to its column's width."""

    def __init__(self, cells: Sequence[Text], columns: Sequence[Column]):
        self.cells, self._columns = cells, columns
        self._fixed = sum((column.width or 0) + column.gap for column in columns)

    @cached_property
    def looks(self) -> tuple:
        return tuple(looks(cell) for cell in self.cells), tuple(self._columns)

    def line(self, width: int) -> Text:
        line = Text(end="", no_wrap=True)
        for cell, column in zip(self.cells, self._columns, strict=True):
            size = max(width - self._fixed, 0) if column.width is None else column.width
            line.append_text(_fit(cell, size, column.justify))
            line.append(" " * column.gap)
        return line


class Line(OneLine):
    """A line across the list in ``char``, after a ``title`` if it has one (as Rich's
    Rule draws one, aligned left)."""

    def __init__(self, char: str, style: str, title: Text | None = None):
        self._char, self._style, self.title = char, style, title

    @cached_property
    def looks(self) -> tuple:
        return self._char, self._style, looks(self.title)

    def line(self, width: int) -> Text:
        if self.title is None:
            return Text(self._char * width, style=self._style, end="")
        title = self.title.copy()
        title.truncate(max(width - 2, 0), overflow="ellipsis")
        line = Text(end="")
        line.append_text(title)
        line.append(" ")
        line.append(self._char * (width - line.cell_len), self._style)
        return line


def looks(prompt: object) -> object:
    """What a list's row shows, comparable with ``==``: a row's :meth:`OneLine.looks`, a
    text's characters and styles, or the row itself (a string)."""
    if isinstance(prompt, OneLine):
        return prompt.looks
    if isinstance(prompt, Text):
        return prompt.plain, str(prompt.style), tuple(prompt.spans)
    return prompt


def _fit(cell: Text, width: int, justify: Literal["left", "right"]) -> Text:
    fitted = cell.copy()
    fitted.truncate(width, overflow="ellipsis")
    room = width - fitted.cell_len
    fitted.pad_left(room) if justify == "right" else fitted.pad_right(room)
    return fitted
