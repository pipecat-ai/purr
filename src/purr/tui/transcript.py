"""The PR's pane (description, diff, conversation): a log you can select text in."""

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog


class Transcript(RichLog):
    """A :class:`RichLog` you can select text in with the mouse.

    Textual's RichLog doesn't mark where each line's text is, so the screen
    can't tell what the mouse is over and selecting does nothing. This marks
    it, like Textual's :class:`~textual.widgets.Log` does.
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(line.text.rstrip() for line in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    def clear(self) -> "Transcript":
        if self.text_selection is not None:
            self.screen.clear_selection()  # the text it was over is gone
        return super().clear()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        index = scroll_y + y
        strip = super().render_line(y)
        if index >= len(self.lines):
            return strip
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(index)) is not None:
            strip = self._highlight(strip, self.lines[index].text.rstrip(), span, scroll_x)
        return strip.apply_offsets(scroll_x, index)

    def _highlight(self, strip: Strip, text: str, span: tuple[int, int], scroll_x: int) -> Strip:
        start, end = span
        if end == -1:
            end = len(text)
        # Characters to cells: wide characters take two.
        start = max(0, cell_len(text[:start]) - scroll_x)
        end = min(strip.cell_length, cell_len(text[:end]) - scroll_x)
        if end <= start:
            return strip
        # Its background only: the theme leaves the text's color as it is ("transparent"),
        # which as a Rich style comes out the same as the background.
        style = Style(bgcolor=self.screen.get_component_rich_style("screen--selection").bgcolor)
        before, selected, after = strip.divide([start, end, strip.cell_length])
        selected = Strip(
            [Segment(t, s + style if s else style, c) for t, s, c in selected],
            selected.cell_length,
        )
        return Strip.join([before, selected, after])
