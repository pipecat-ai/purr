"""The PR session's prompt: a shell-style line with history.

↑/↓ go through what you asked on this PR, ctrl+r searches everything you've
asked on any PR, and the usual readline keys move around the line (ctrl+a/e,
ctrl+b/f, alt+b/f, plus Textual's ctrl+k/u/w).
"""

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from purr.models import PRRef


class PromptInput(Input):
    """An input line with history, newest last, like a shell's."""

    BINDINGS = [
        Binding("up", "history(-1)", "Older", show=False),
        Binding("down", "history(1)", "Newer", show=False),
        Binding("ctrl+b", "cursor_left", "Back a character", show=False),
        Binding("ctrl+f", "cursor_right", "Forward a character", show=False),
        Binding("alt+b", "cursor_left_word", "Back a word", show=False),
        Binding("alt+f", "cursor_right_word", "Forward a word", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._position = 0  # len(history): the line being written
        self._draft = ""  # that line, while looking back

    def set_history(self, history: list[str]) -> None:
        """What was asked, oldest first."""
        self._history = list(history)
        self._position = len(self._history)
        self._draft = ""

    def remember(self, text: str) -> None:
        """Add what was just sent, and go back to a fresh line."""
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._position = len(self._history)
        self._draft = ""

    def action_history(self, step: int) -> None:
        position = min(max(self._position + step, 0), len(self._history))
        if position == self._position:
            return
        if self._position == len(self._history):
            self._draft = self.value
        self._position = position
        self.value = self._history[position] if position < len(self._history) else self._draft
        self.cursor_position = len(self.value)


def short_ref(ref: PRRef) -> str:
    return f"{ref.repo.split('/')[-1]}#{ref.number}"


class HistorySearchScreen(ModalScreen[str | None]):
    """ctrl+r: find something you asked before, on any PR."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up,ctrl+p", "move(-1)", "Newer", show=False),
        Binding("down,ctrl+n,ctrl+r", "move(1)", "Older", show=False),
    ]

    def __init__(self, prompts: list[tuple[PRRef, str]], query: str = ""):
        super().__init__()
        seen: set[str] = set()
        self._prompts = []  # newest first, each text once
        for ref, text in prompts:
            if text not in seen:
                seen.add(text)
                self._prompts.append((ref, text))
        self._query = query

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="history"):
            yield Label("Search what you've asked", classes="hint")
            yield Input(value=self._query, placeholder="Type to search…", id="history-query")
            yield OptionList(id="history-items")

    def on_mount(self) -> None:
        self._show(self._query)

    @on(Input.Changed, "#history-query")
    def _typed(self, event: Input.Changed) -> None:
        self._show(event.value)

    def _show(self, query: str) -> None:
        words = query.lower().split()
        options = [
            Option(Text.assemble((f"{short_ref(ref):<18}", "dim"), text), id=str(i))
            for i, (ref, text) in enumerate(self._prompts)
            if all(word in text.lower() for word in words)
        ]
        items = self.query_one(OptionList)
        items.set_options(options or [Option(Text("Nothing matches.", style="dim"), disabled=True)])
        items.highlighted = 0 if options else None

    def action_move(self, step: int) -> None:
        items = self.query_one(OptionList)
        items.action_cursor_down() if step > 0 else items.action_cursor_up()

    @on(Input.Submitted, "#history-query")
    @on(OptionList.OptionSelected, "#history-items")
    def _picked(self) -> None:
        option = self.query_one(OptionList).highlighted_option
        self.dismiss(self._prompts[int(option.id)][1] if option and option.id else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
