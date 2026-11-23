"""A text box with Emacs's keys, for everywhere Purr has you write.

The prompt, the comment editor and the file editor are each one of these. Besides
Textual's own keys (the arrows, home/end, shift to select, ctrl+z undo, ctrl+x/c/v
cut, copy and paste), the ones from Emacs and readline: ctrl+a/e the start and end
of the line, ctrl+b/f and alt+b/f a character or a word, ctrl+p/n a line up or
down, alt+</alt+> the start and end of the text, ctrl+k/u/w and alt+d kill, ctrl+y
yanks what was killed, ctrl+_ undoes.
"""

from textual.binding import Binding
from textual.widgets import TextArea


class EmacsArea(TextArea):
    """A :class:`TextArea` with Emacs's keys too (see the module)."""

    BINDINGS = [
        Binding("ctrl+p", "cursor_up", "Up a line", show=False),
        Binding("ctrl+n", "cursor_down", "Down a line", show=False),
        Binding("ctrl+a", "line_start", "Start of the line", show=False),
        Binding("ctrl+b", "cursor_left", "Back a character", show=False),
        Binding("ctrl+f", "cursor_right", "Forward a character", show=False),
        Binding("alt+b", "cursor_word_left", "Back a word", show=False),
        Binding("alt+f", "cursor_word_right", "Forward a word", show=False),
        Binding("alt+less_than_sign", "text_start", "Start of the text", show=False),
        Binding("alt+greater_than_sign", "text_end", "End of the text", show=False),
        Binding("ctrl+k", "kill_line", "Kill to the end of the line", show=False),
        Binding("ctrl+u", "kill_line_start", "Kill to the start of the line", show=False),
        Binding("ctrl+w,alt+backspace", "kill_word_left", "Kill a word back", show=False),
        Binding("alt+d", "kill_word_right", "Kill a word forward", show=False),
        Binding("ctrl+y", "yank", "Yank what was killed", show=False),
        Binding("ctrl+underscore", "undo", "Undo", show=False),
    ]
    TAKES_OVER = {"next_alert", "command_palette"}  # the app's ctrl+n/p: lines, here

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._killed = ""  # what ctrl+k, ctrl+u, ctrl+w or alt+d took last, for ctrl+y

    def action_line_start(self) -> None:
        """Its very start (Textual's ctrl+a stops at the first non-blank, as home does)."""
        self.move_cursor((self.cursor_location[0], 0))

    def action_text_start(self) -> None:
        self.move_cursor((0, 0))

    def action_text_end(self) -> None:
        self.move_cursor(self.document.end)

    def action_kill_line(self) -> None:
        """To the end of the line; at its end, the line break."""
        row, column = self.cursor_location
        end = self.get_cursor_line_end_location()
        if end == (row, column) and row + 1 < self.document.line_count:
            end = (row + 1, 0)
        self._kill(end)

    def action_kill_line_start(self) -> None:
        self._kill(self.get_cursor_line_start_location())

    def action_kill_word_left(self) -> None:
        self._kill(self.get_cursor_word_left_location())

    def action_kill_word_right(self) -> None:
        self._kill(self.get_cursor_word_right_location())

    def _kill(self, to: tuple[int, int]) -> None:
        """Delete from the cursor to ``to``, kept for ctrl+y."""
        start, end = sorted((self.cursor_location, to))
        if start != end:
            self._killed = self.get_text_range(start, end)
            self.delete(start, end)

    def action_yank(self) -> None:
        if self._killed:
            self.insert(self._killed)
