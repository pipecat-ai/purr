"""The PR's pane (its description, the conversation): a log you can select text in."""

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.timer import Timer
from textual.widgets import RichLog

from purr.tui.scrolling import glide_by, glide_to, page_lines

REWRAP_AFTER = 0.15  # seconds of no more resizing (dragging a terminal's edge sends many)


class Transcript(RichLog):
    """A :class:`RichLog` you can select text in with the mouse.

    Textual's RichLog doesn't mark where each line's text is, so the screen
    can't tell what the mouse is over and selecting does nothing. This marks
    it, like Textual's :class:`~textual.widgets.Log` does.
    """

    class Rewrap(Message):
        """Its width changed: what's written is wrapped for the old one (RichLog wraps a
        line once, as it's written), so whoever wrote it should write it again."""

    _width: int | None = None
    _rewrap: Timer | None = None
    _at_end = True  # scrolled to the end, where it stays when it gets shorter
    _hidden_writes = False  # written to while hidden: wrapped for no width

    def write(self, *args, **kwargs) -> "Transcript":
        # Hidden (the diff zoomed in its place), it has no width, and RichLog wraps what's
        # written then at its minimum (78), however wide it is when it shows again. Before
        # its first size, RichLog keeps what's written for later on its own.
        if self._size_known and not self.scrollable_content_region.width:
            self._hidden_writes = True
        return super().write(*args, **kwargs)

    def on_show(self) -> None:
        if self._hidden_writes:
            self._hidden_writes = False
            self.post_message(self.Rewrap())

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._at_end = new_value >= self.max_scroll_y

    # Keys scroll it smoothly (see purr.tui.scrolling): Textual's own take a second to
    # get to the top or the end, and a page goes at 50 lines a second.
    def page(self, step: int) -> None:
        """A page down (1) or up (-1)."""
        glide_by(self, step * page_lines(self))

    def action_page_down(self) -> None:
        self.page(1)

    def action_page_up(self) -> None:
        self.page(-1)

    def action_scroll_down(self) -> None:
        glide_by(self, 1)

    def action_scroll_up(self) -> None:
        glide_by(self, -1)

    def action_scroll_home(self) -> None:
        glide_to(self, 0)

    def action_scroll_end(self) -> None:
        glide_to(self, self.max_scroll_y)

    def on_resize(self, event: events.Resize) -> None:
        if self._at_end:  # shorter (the decision box came back): the last lines stay in view
            self.scroll_end(animate=False)
        # Its whole width: a scrollbar coming or going mustn't start another rewrap.
        width = self.outer_size.width
        if self._width not in (None, width):
            if self._rewrap:
                self._rewrap.stop()
            self._rewrap = self.set_timer(REWRAP_AFTER, lambda: self.post_message(self.Rewrap()))
        self._width = width

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
