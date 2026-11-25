"""Purr's editor, for your files: instructions, permissions, settings.

Full screen, in Purr, with the keys of the prompt (Emacs's, and Textual's: shift to
select, ctrl+x/c/v, ctrl+z): ``ctrl+s`` saves, ``esc`` closes (asking first if
there are changes it hasn't saved). A file Purr reads (settings, permissions) is
checked before it's saved, so a typo shows at once and never reaches the file.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from purr.tui.dialogs import ConfirmScreen
from purr.tui.editing import EmacsArea
from purr.tui.keybar import KeyBar

Check = Callable[[str], object]  # raises ValueError, saying what's wrong
Saved = Callable[[str], Awaitable[None]]  # what saving it does at once, besides

_LANGUAGES = {".md": "markdown", ".toml": "toml", ".json": "json"}


def check_json(text: str) -> None:
    """A JSON file's text, as Claude Code's settings are: ValueError, saying where, if not."""
    try:
        json.loads(text or "{}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Line {e.lineno}, column {e.colno}: {e.msg}") from e


def shown(path: Path) -> str:
    """``~/.config/purr/review.md`` rather than the whole of it."""
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


class EditorArea(EmacsArea):
    """The file's text: tab indents; esc is the screen's, which closes it."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.prevent_default()  # not TextArea's (the focus elsewhere): it bubbles


class EditorScreen(Screen[bool]):
    """A file of yours, in Purr. Dismisses with whether it was saved."""

    DEFAULT_CSS = """
    #editor {
        border: round $accent;
        border-title-color: $accent;
        border-subtitle-color: $text-muted;
        padding: 0 1;
    }
    #editor-text { height: 1fr; border: none; padding: 0; }
    #editor-status { height: 1; padding: 0 2; color: $text-muted; }
    #editor-status.-error { color: $error; }
    #editor-status.-saved { color: $success; }
    """
    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(
        self,
        path: Path,
        *,
        about: str,
        applies: str = "from the agents' next turn",
        check: Check | None = None,
        saved: Saved | None = None,
    ):
        super().__init__()
        self.path = path
        self._about, self._applies = about, applies
        self._check, self._saved = check, saved
        self._text = path.read_text() if path.exists() else ""  # as it is in the file
        self._wrote = False

    def compose(self) -> ComposeResult:
        language = _LANGUAGES.get(self.path.suffix)
        area = EditorArea(self._text, soft_wrap=self.path.suffix == ".md", tab_behavior="indent")
        area.show_line_numbers = True
        if language in area.available_languages:
            area.language = language
        area.id = "editor-text"
        with Vertical(id="editor"):
            yield area
        yield Static(id="editor-status")
        yield KeyBar()

    def on_mount(self) -> None:
        box = self.query_one("#editor")
        box.border_title = shown(self.path)
        box.border_subtitle = self._about
        self._say(f"It applies {self._applies} once saved.")
        self.query_one(EditorArea).focus()

    def _say(self, text: str, kind: str = "") -> None:
        status = self.query_one("#editor-status", Static)
        status.update(Text(text))
        status.set_classes(f"-{kind}" if kind else "")

    @property
    def changed(self) -> bool:
        """What's written differs from the file."""
        return self.query_one(EditorArea).text != self._text

    async def action_save(self) -> None:
        text = self.query_one(EditorArea).text
        try:
            if self._check:
                self._check(text)
        except ValueError as e:
            self._say(f"Not saved: {e}", "error")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)
        self._text, self._wrote = text, True
        self._say(f"Saved. It applies {self._applies}.", "saved")
        if self._saved:
            await self._saved(text)

    def action_close(self) -> None:
        if not self.changed:
            self.dismiss(self._wrote)
            return

        def answered(close: bool | None) -> None:
            if close:
                self.dismiss(self._wrote)

        question = ConfirmScreen("Close without saving?", "What you changed since is lost.")
        self.app.push_screen(question, answered)
