"""``?`` on any page: every key that works where you are.

Made from the bindings themselves (what Textual says is active at the focused
widget, and enabled), so it can't drift from what the keys do. The footer shows
what fits; this shows the rest: keys that work everywhere, keys the pane hides
to make room, keys for the repo around it. Each page's own bindings start with
``KEYS``, so the footer shows ``? Keys`` before it runs out of room.
"""

from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, Static

if TYPE_CHECKING:
    from textual.app import App

KEYS = Binding("question_mark", "app.keys", "Keys", key_display="?")

# What to call the keys of each part of Purr, by the class that binds them.
_SECTIONS = {
    "DiffView": "The diff",
    "DiffPane": "The diff",
    "PromptArea": "Writing to the agent",
    "PRSession": "The PR",
    "PRList": "The PR list",
    "ReposView": "Home",
    "RepoView": "The repo",
    "PurrApp": "Everywhere",
}


def key_groups(app: "App") -> list[tuple[str, list[tuple[str, str]]]]:
    """``(section, [(keys, what they do)])``, from where the focus is outwards."""
    groups: dict[str, dict[str, list[str]]] = {}
    for active in app.screen.active_bindings.values():
        node, binding = active.node, active.binding
        if not active.enabled or not binding.description or not _ours(node, binding):
            continue
        section = _SECTIONS.get(type(node).__name__, type(node).__name__)
        describe = getattr(app.screen, "describe_key", None)  # what some say depends
        what = describe(binding) if describe else binding.description
        keys = groups.setdefault(section, {}).setdefault(what, [])
        keys.append(app.get_key_display(binding))
    return [
        (section, [(_together(keys), what) for what, keys in rows.items()])
        for section, rows in groups.items()
    ]


def _ours(node: object, binding: Binding) -> bool:
    """Purr's own bindings, not Textual's widgets' (a list's arrows, a text box's), nor
    those Purr's widgets inherit from them."""
    return any(
        cls.__module__.startswith("purr.") and _declares(cls, binding) for cls in type(node).__mro__
    )


def _declares(cls: type, binding: Binding) -> bool:
    """Whether the class's own BINDINGS has it (Textual copies them, one per key)."""
    own = [b for b in cls.__dict__.get("BINDINGS", []) if isinstance(b, Binding)]
    return any((b.action, b.description) == (binding.action, binding.description) for b in own)


def _together(keys: list[str]) -> str:
    """``tab``, ``pgdn ^v``, or ``1–9`` for a run of them."""
    keys = list(dict.fromkeys(keys))
    return f"{keys[0]}–{keys[-1]}" if len(keys) > 4 else "  ".join(keys)


class KeysScreen(ModalScreen[None]):
    """The keys for where you were when you pressed ``?``."""

    DEFAULT_CSS = """
    KeysScreen { align: center middle; }
    #keys { width: 76; max-height: 90%; height: auto; }
    #keys-list { height: auto; max-height: 40; margin: 1 0; }
    """
    BINDINGS = [Binding("escape,question_mark,q", "close", "Close")]

    def __init__(self, groups: list[tuple[str, list[tuple[str, str]]]]):
        super().__init__()
        self._groups = groups

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog", id="keys"):
            yield Label("Keys, for where you are")
            yield Static(self._table(), id="keys-list")
            yield Label("esc or ? closes · the README has the rest", classes="hint")

    def _table(self) -> Table:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", no_wrap=True)
        table.add_column()
        for section, rows in self._groups:
            table.add_row(Text(section, style="bold underline"), "")
            for keys, what in rows:
                table.add_row(keys, what)
            table.add_row("", "")
        return table

    def action_close(self) -> None:
        self.dismiss(None)
