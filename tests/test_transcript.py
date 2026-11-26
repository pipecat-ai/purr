"""The PR's conversation as a log: what stays in view when its space changes."""

import asyncio

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from purr.tui.transcript import Transcript


class Pane(App):
    """A transcript, with a box under it that comes and goes (as the decision box does)."""

    CSS = "Transcript { height: 1fr; } #box { height: 5; display: none; }"

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Transcript(auto_scroll=True)
            yield Static("box", id="box")


async def until(pilot, condition) -> None:
    async def wait():
        while not condition():
            await pilot.pause(0.02)

    await asyncio.wait_for(wait(), 5)


async def _written(pilot, log: Transcript) -> None:
    for n in range(40):
        log.write(f"line {n}")
    # A RichLog adds what's written, and scrolls to its end, after a refresh.
    await until(pilot, lambda: log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y)


async def test_the_last_lines_stay_in_view_when_it_gets_shorter():
    app = Pane()
    async with app.run_test(size=(40, 20)) as pilot:
        log = app.query_one(Transcript)
        await _written(pilot, log)
        app.query_one("#box").display = True
        await until(pilot, lambda: log.size.height < 20)
        await until(pilot, lambda: log.scroll_y == log.max_scroll_y)  # "line 39" still shows


async def test_where_you_scrolled_to_stays_when_it_gets_shorter():
    app = Pane()
    async with app.run_test(size=(40, 20)) as pilot:
        log = app.query_one(Transcript)
        await _written(pilot, log)
        log.scroll_to(y=3, animate=False)
        await until(pilot, lambda: log.scroll_y == 3)
        app.query_one("#box").display = True
        await until(pilot, lambda: log.size.height < 20)
        await pilot.pause(0.1)  # a few refreshes: if it were to scroll, it would have
        assert log.scroll_y == 3


async def test_keys_glide_far_and_move_a_line_at_once():
    import time

    from purr.tui.scrolling import GLIDE_SECONDS, page_lines

    app = Pane()
    async with app.run_test(size=(40, 20)) as pilot:
        log = app.query_one(Transcript)
        for n in range(100):
            log.write(f"line {n}")
        await until(pilot, lambda: log.max_scroll_y > 0 and log.scroll_y == log.max_scroll_y)
        log.focus()
        seen: list[float] = []
        log.watch(log, "scroll_y", lambda y: seen.append(y), init=False)
        # To the top: it glides there, through the lines between, in GLIDE_SECONDS
        # (Textual's own takes a second).
        start = time.monotonic()
        await pilot.press("home")
        await until(pilot, lambda: log.scroll_y == 0)
        assert time.monotonic() - start < GLIDE_SECONDS + 0.3
        assert len(set(seen)) > 3
        # A line: at once.
        seen.clear()
        await pilot.press("down")
        await until(pilot, lambda: log.scroll_y == 1)
        assert seen == [1]
        # Pages glide; another on the way goes on from where the first was going.
        seen.clear()
        log.page(1)
        log.page(1)
        await until(pilot, lambda: log.scroll_y == 1 + 2 * page_lines(log))
        assert len(set(seen)) > 3
        await pilot.press("end")
        await until(pilot, lambda: log.scroll_y == log.max_scroll_y)
