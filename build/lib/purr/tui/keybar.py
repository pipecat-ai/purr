"""The footer: every key that works where you are, on as many lines as that takes.

Textual's footer is one line, which a pane's keys outgrow; this one wraps, never in
the middle of a key. It shows what the screen's bindings say (``show``), from the
focused widget outwards (the pane's keys, the repo's, then those that work
everywhere), each key once: a key two panes bind shows the nearer one. Keys in a
group (``n p Next/prev``) go together, a run of them as ``1–9``.
"""

from dataclasses import replace
from itertools import groupby

from rich.cells import cell_len
from textual.content import Content
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Static

_GAP = " │ "  # between keys


class KeyBar(Static):
    """Every key that works where you are (see the module)."""

    DEFAULT_CSS = """
    KeyBar {
        dock: bottom;
        height: auto;
        padding: 0 1;
        color: $footer-description-foreground;
        background: $footer-background;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.keys: list[tuple[str, str]] = []  # (keys, what they do), as shown

    def on_mount(self) -> None:
        self._screen = self.screen  # its own, to let go of when it goes
        self._screen.bindings_updated_signal.subscribe(self, self._changed)

    def on_unmount(self) -> None:
        self._screen.bindings_updated_signal.unsubscribe(self)

    def on_resize(self) -> None:
        self._draw()

    def _changed(self, screen: Screen) -> None:
        if screen is self._screen:
            self.call_after_refresh(self._draw)

    def _draw(self) -> None:
        if not self.is_attached:  # its screen closed (the editor) while a redraw waited
            return
        self.keys = self._here()
        lines = _pack([f"{keys} {what}" for keys, what in self.keys], self.content_size.width)
        self.update(Content.from_markup("\n".join(_markup(self.keys, line) for line in lines)))

    def _here(self) -> list[tuple[str, str]]:
        """``(keys, what they do)`` for each key shown here, in order."""
        shown, seen = [], set()
        describe = getattr(self.screen, "describe_key", None)  # what some say depends
        for active in self.screen.active_bindings.values():
            binding = active.binding
            if not (binding.show and active.enabled and binding.description):
                continue
            if describe:
                binding = replace(binding, description=describe(binding))
            if binding.action not in seen:  # one key per action: the nearest, first
                seen.add(binding.action)
                shown.append((binding, self.app.get_key_display(binding)))
        keys: list[tuple[str, str]] = []
        for group, members in groupby(shown, lambda pair: pair[0].group):
            keys += _together(list(members), group)
        return keys


def _together(members: list, group) -> list[tuple[str, str]]:
    """Each key's ``(keys, what)``; a group's keys together, with its description."""
    if group is None or len(members) == 1:
        return [(keys, binding.description) for binding, keys in members]
    keys = list(dict.fromkeys(display for _, display in members))
    shown = f"{keys[0]}–{keys[-1]}" if len(keys) > 4 else " ".join(keys)
    return [(shown, group.description)]


def _pack(items: list[str], width: int) -> list[list[int]]:
    """Which items go on each line (by index), as many as fit, none split."""
    lines: list[list[int]] = [[]]
    used = 0
    for i, item in enumerate(items):
        size = cell_len(item) + (cell_len(_GAP) if lines[-1] else 0)
        if lines[-1] and width and used + size > width:
            lines.append([])
            size, used = cell_len(item), 0
        lines[-1].append(i)
        used += size
    return lines


def _markup(keys: list[tuple[str, str]], line: list[int]) -> str:
    gap = f"[dim]{escape(_GAP)}[/dim]"
    return gap.join(
        f"[bold $footer-key-foreground]{escape(keys[i][0])}[/] {escape(keys[i][1])}" for i in line
    )
