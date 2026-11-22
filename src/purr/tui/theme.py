"""Purr's colors: Solarized (https://ethanschoonover.com/solarized/).

Textual's own ``solarized-dark`` theme does the widgets; the named colors in
Purr's text ("green", "red", ...) map to Solarized's terminal palette, and the
few colors Purr picks itself come from the same sixteen.
"""

from rich.terminal_theme import TerminalTheme
from textual.reactive import Reactive

BASE03, BASE02, BASE01, BASE00 = "#002b36", "#073642", "#586e75", "#657b83"
BASE0, BASE1, BASE2, BASE3 = "#839496", "#93a1a1", "#eee8d5", "#fdf6e3"
YELLOW, ORANGE, RED, MAGENTA = "#b58900", "#cb4b16", "#dc322f", "#d33682"
VIOLET, BLUE, CYAN, GREEN = "#6c71c4", "#268bd2", "#2aa198", "#859900"

THEME = "solarized-dark"
CODE_THEME = "solarized-dark"  # Pygments', for code in descriptions and replies


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


# Solarized's terminal palette: the eight colors, then the bright eight.
_NORMAL = [_rgb(c) for c in (BASE02, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, BASE2)]
_BRIGHT = [_rgb(c) for c in (BASE03, ORANGE, BASE01, BASE00, BASE0, VIOLET, BASE1, BASE3)]


def ansi_themes() -> tuple[Reactive, Reactive]:
    """For :class:`~textual.app.App`'s ``ansi_theme_dark`` and ``ansi_theme_light``."""
    dark = TerminalTheme(_rgb(BASE03), _rgb(BASE0), _NORMAL, _BRIGHT)
    light = TerminalTheme(_rgb(BASE3), _rgb(BASE00), _NORMAL, _BRIGHT)
    return Reactive(dark, init=False), Reactive(light, init=False)
