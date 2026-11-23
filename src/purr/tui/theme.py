"""Purr's colors: a Textual theme's (the ``theme`` setting; Solarized dark by default).

The theme does the widgets. The named colors in Purr's text ("green", "red", ...)
are the terminal's sixteen as the theme has them: Solarized's palette under its two
Solarized themes, Textual's own under the others. And the few colors Purr picks
itself (:class:`Palette`) are Solarized's under those two, the theme's otherwise.
"""

from dataclasses import dataclass

from rich.terminal_theme import TerminalTheme
from textual.color import Color

BASE03, BASE02, BASE01, BASE00 = "#002b36", "#073642", "#586e75", "#657b83"
BASE0, BASE1, BASE2, BASE3 = "#839496", "#93a1a1", "#eee8d5", "#fdf6e3"
YELLOW, ORANGE, RED, MAGENTA = "#b58900", "#cb4b16", "#dc322f", "#d33682"
VIOLET, BLUE, CYAN, GREEN = "#6c71c4", "#268bd2", "#2aa198", "#859900"

DEFAULT = "solarized-dark"


@dataclass(frozen=True)
class Palette:
    """The colors Purr picks itself, besides the theme's widgets'."""

    name: str  # what a list row is about (a PR's title, a repo's name): bright
    number: str  # PR numbers, wherever they show
    box: str  # a comment box's inside, in the diff
    working: str  # the spinning mark and line while an agent's at work
    shine: str  # the shine sweeping across that line
    code: str  # Pygments' style, for code in descriptions and replies, and in the diff
    added: str  # behind a line the diff adds: green, faint
    removed: str  # behind one it removes: red, faint


# How much green, or red, goes into the background behind a changed line. Red more on a
# dark background, or against a blue-green one (Solarized's) it comes out grey; on a
# light one, that much is loud.
ADDED_TINT, REMOVED_TINT, REMOVED_TINT_LIGHT = 0.2, 0.32, 0.2


def _removed_tint(dark: bool) -> float:
    return REMOVED_TINT if dark else REMOVED_TINT_LIGHT


def _tint(background: str, color: str, amount: float) -> str:
    """The background, with a little of ``color`` in it."""
    return Color.parse(background).blend(Color.parse(color), amount).hex6


def _solarized(background: str, *, dark: bool) -> Palette:
    return Palette(
        name=BASE2 if dark else BASE02,
        number=BLUE,
        box=BASE02 if dark else BASE2,
        working=ORANGE,
        shine="#e19872" if dark else "#a33c12",
        code="solarized-dark" if dark else "solarized-light",
        added=_tint(background, GREEN, ADDED_TINT),
        removed=_tint(background, RED, _removed_tint(dark)),
    )


_SOLARIZED = {
    "solarized-dark": _solarized(BASE03, dark=True),
    "solarized-light": _solarized(BASE3, dark=False),
}

# Pygments' style for a theme that has one; else github-dark, or default when light.
_CODE = {
    "atom-one-dark": "one-dark",
    "dracula": "dracula",
    "gruvbox": "gruvbox-dark",
    "monokai": "monokai",
    "nord": "nord",
    "tokyo-night": "night-owl",
}


def theme_names() -> list[str]:
    """What the ``theme`` setting can be: Textual's themes."""
    from textual.theme import BUILTIN_THEMES

    return sorted(BUILTIN_THEMES)


_palette = _SOLARIZED[DEFAULT]


def palette() -> Palette:
    """The colors Purr picks, for the theme in use (see :func:`use`)."""
    return _palette


def use(theme: str, variables: dict[str, str], *, dark: bool) -> None:
    """The theme the app's in, with its CSS variables (``App.get_css_variables``)."""
    global _palette
    _palette = _SOLARIZED.get(theme) or _from_theme(theme, variables, dark=dark)


def _from_theme(theme: str, variables: dict[str, str], *, dark: bool) -> Palette:
    """Purr's colors from a theme's: its text a little brighter (darker, when light), its
    primary color for numbers, its panel for boxes, its warning color for work, and its
    background tinted with its success and error colors for the diff's lines."""
    shade = 0.15 if dark else -0.15
    working = Color.parse(variables["warning"])
    return Palette(
        name=Color.parse(variables["foreground"]).lighten(shade).hex6,
        number=Color.parse(variables["primary"]).hex6,
        box=Color.parse(variables["panel"]).hex6,
        working=working.hex6,
        shine=working.lighten(shade).hex6,
        code=_CODE.get(theme, "github-dark" if dark else "default"),
        added=_tint(variables["background"], variables["success"], ADDED_TINT),
        removed=_tint(variables["background"], variables["error"], _removed_tint(dark)),
    )


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


# Solarized's terminal palette: the eight colors, then the bright eight.
_NORMAL = [_rgb(c) for c in (BASE02, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, BASE2)]
_BRIGHT = [_rgb(c) for c in (BASE03, ORANGE, BASE01, BASE00, BASE0, VIOLET, BASE1, BASE3)]


def ansi_themes(theme: str) -> tuple[TerminalTheme, TerminalTheme]:
    """What the named colors are, dark and light (for :class:`~textual.app.App`'s
    ``ansi_theme_dark`` and ``ansi_theme_light``): Solarized's for its themes, Textual's
    own for the others."""
    if theme not in _SOLARIZED:
        from textual._ansi_theme import ALABASTER, MONOKAI  # what App starts with

        return MONOKAI, ALABASTER
    dark = TerminalTheme(_rgb(BASE03), _rgb(BASE0), _NORMAL, _BRIGHT)
    light = TerminalTheme(_rgb(BASE3), _rgb(BASE00), _NORMAL, _BRIGHT)
    return dark, light
