"""The line that moves while the agent works, like Claude Code's spinner.

A review can go quiet for minutes (agents working in the background, a slow
test run) and a still screen reads as frozen. This line keeps moving while
Purr waits on the agent, and goes away when the agent is done or waiting on you.
It moves too while Purr does what you asked for (a push, a merge), which can take
a few seconds: ``✻ Pushing… (2s)``.
"""

import time

from rich.text import Text
from textual.widgets import Static

from purr.tui.theme import palette

# A star that grows and shrinks.
FRAMES = "·✢✳✶✻✽✻✶✳✢"
# What the agent is up to, as far as the cat is concerned. One per stretch of
# work, changing every so often on long ones.
VERBS = [
    "Purring",
    "Prowling",
    "Pouncing",
    "Kneading",
    "Sniffing around",
    "Stalking bugs",
    "Pawing through",
    "Whisker-twitching",
    "Chasing threads",
    "Batting at it",
    "Hunting",
    "Circling",
    "Nosing around",
    "Tail-flicking",
    "Perching",
    "Mulling",
]
VERB_SECONDS = 20  # how long a verb lasts
FRAME_SECONDS = 0.12
_SHINE = 3  # letters lit at once, sweeping across the verb


def elapsed_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def render_working(
    since: float, now: float, frame: int, background: int = 0, doing: str = ""
) -> Text:
    """``✻ Prowling… (1m 12s · 2 in the background · esc to stop)``, with a shine sweeping
    across the verb. For what you asked for, what it is: ``✻ Pushing… (2s)``."""
    elapsed = max(0.0, now - since)
    word = f"{doing or _verb(since, elapsed)}…"
    colors = palette()
    text = Text(f"{FRAMES[frame % len(FRAMES)]} ", style=f"bold {colors.working}")
    lit = frame % (len(word) + 2 * _SHINE) - _SHINE
    for i, char in enumerate(word):
        lit_up = lit <= i < lit + _SHINE
        text.append(char, style=f"bold {colors.shine}" if lit_up else colors.working)
    details = elapsed_text(elapsed)
    if not doing:  # the agent's: esc stops it
        details += f" · {background} in the background" if background else ""
        details += " · esc to stop"
    text.append(f" ({details})", style="dim")
    return text


def _verb(since: float, elapsed: float) -> str:
    """What the agent's up to, seeded by when the stretch began: each starts on its own."""
    return VERBS[(int(since * 10) + int(elapsed // VERB_SECONDS)) % len(VERBS)]


class Working(Static):
    """Shown, and moving, while the agent works on the PR in view, or Purr does what you
    asked for on it."""

    def on_mount(self) -> None:
        self.display = False
        self._since: float | None = None
        self._background = 0  # tasks out in the background
        self._doing = ""  # what you asked for (``Pushing``), or "" for the agent at work
        self._frame = 0
        self._timer = self.set_interval(FRAME_SECONDS, self._tick, pause=True)

    @property
    def since(self) -> float | None:
        return self._since

    def start(self, since: float, background: int = 0, doing: str = "") -> None:
        """Working since ``since`` (``time.monotonic()``), with ``background`` tasks out;
        or doing what you asked for (``doing``) since then."""
        if self._since != since:
            self._since = since
            self._frame = 0
        self._background, self._doing = background, doing
        self._draw()
        if not self.display:
            self.display = True
            self._timer.resume()

    def stop(self) -> None:
        self._since = None
        if self.display:
            self.display = False
            self._timer.pause()

    def set_background(self, background: int) -> None:
        self._background = background
        self._draw()

    def _tick(self) -> None:
        self._frame += 1
        self._draw()

    def _draw(self) -> None:
        if self._since is not None:
            self.update(
                render_working(
                    self._since, time.monotonic(), self._frame, self._background, self._doing
                )
            )
