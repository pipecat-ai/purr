"""The PR session's prompt: several lines, with a shell-style history.

``enter`` sends; ``ctrl+j`` (or ``shift+enter``/``alt+enter`` where the terminal
tells them apart, or a ``\\`` at the end of the line) starts a new line, and
pasting keeps the lines. ↑/↓ on the first/last line go through what you asked on
this PR, ctrl+r searches everything you've asked on any PR, and the usual
readline keys move around (ctrl+a/e, ctrl+b/f, alt+b/f, ctrl+k/u/w).

Typing ``/`` shows matching commands (see :mod:`purr.tui.commands`): ↑/↓ pick
one, tab or enter fills it in, and enter again runs it.

Once the agent's done, the empty prompt shows what you might ask next, dim, as
Claude Code does (see :mod:`purr.suggest`): tab or → takes it.
"""

from dataclasses import dataclass
from typing import Protocol

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, TextArea
from textual.widgets.option_list import Option

from purr.models import PRRef

NEWLINE_KEYS = ("ctrl+j", "shift+enter", "alt+enter")
PLACEHOLDER = "Ask the agent about this PR…  (/ commands, ctrl+j: new line)"


class Completer(Protocol):
    """What the prompt asks while it shows suggestions (the PR session provides it)."""

    def active(self) -> bool: ...

    def move(self, step: int) -> None: ...

    def accept(self) -> str | None: ...


class PromptArea(TextArea):
    """Where you type to the PR's agent: several lines, and history newest last."""

    @dataclass
    class Submitted(Message):
        prompt: "PromptArea"
        text: str

    BINDINGS = [
        Binding("up", "older", "Older", show=False),
        Binding("down", "newer", "Newer", show=False),
        Binding("ctrl+b", "cursor_left", "Back a character", show=False),
        Binding("ctrl+f", "cursor_right", "Forward a character", show=False),
        Binding("alt+b", "cursor_word_left", "Back a word", show=False),
        Binding("alt+f", "cursor_word_right", "Forward a word", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            placeholder=PLACEHOLDER,
            **kwargs,
        )
        self.suggestion = ""  # what you might ask next: shown while it's empty
        self._history: list[str] = []
        self._position = 0  # len(history): the text being written
        self._draft = ""  # that text, while looking back
        self.completer: Completer | None = None

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("tab", "right") and self._take_suggestion():
            event.stop()
            event.prevent_default()
            return
        if event.key in ("tab", "enter") and self.completer and self.completer.active():
            choice = self.completer.accept()
            # Tab fills in the highlighted command; so does enter, unless that's
            # already what's typed (then it runs).
            if choice and (event.key == "tab" or choice != self.text.strip()):
                event.stop()
                event.prevent_default()
                self.set_text(choice + " ")
                return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            row, column = self.cursor_location
            if column and self.document.get_line(row)[column - 1] == "\\":
                # Like Claude Code: a backslash at the end continues on a new line.
                self.delete((row, column - 1), (row, column))
                self.insert("\n")
            else:
                self.post_message(self.Submitted(self, self.text))
            return
        if event.key in NEWLINE_KEYS:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    def suggest(self, text: str) -> None:
        """What you might ask next, dim while the prompt's empty; "" for none."""
        self.suggestion = text
        self.placeholder = f"{text}   (tab)" if text else PLACEHOLDER

    def _take_suggestion(self) -> bool:
        """Tab (or →) on the empty prompt takes the suggestion, as in Claude Code."""
        if self.text or not self.suggestion:
            return False
        self.set_text(self.suggestion)
        return True

    def set_text(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

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

    def action_older(self) -> None:
        if self.completer and self.completer.active():
            self.completer.move(-1)
        elif self.cursor_location[0] > 0:
            self.action_cursor_up()
        else:
            self._step(-1)

    def action_newer(self) -> None:
        if self.completer and self.completer.active():
            self.completer.move(1)
        elif self.cursor_location[0] < self.document.line_count - 1:
            self.action_cursor_down()
        else:
            self._step(1)

    def _step(self, step: int) -> None:
        position = min(max(self._position + step, 0), len(self._history))
        if position == self._position:
            return
        if self._position == len(self._history):
            self._draft = self.text
        self._position = position
        self.set_text(self._history[position] if position < len(self._history) else self._draft)


def _one_line(text: str) -> str:
    return " ⏎ ".join(line for line in text.splitlines() if line.strip())


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
            Option(Text.assemble((f"{short_ref(ref):<18}", "dim"), _one_line(text)), id=str(i))
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
