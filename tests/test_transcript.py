"""The PR's conversation as a log: what stays in view when its space changes."""

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


async def _written(pilot, log: Transcript) -> None:
    for n in range(40):
        log.write(f"line {n}")
    await pilot.pause()


async def test_the_last_lines_stay_in_view_when_it_gets_shorter():
    app = Pane()
    async with app.run_test(size=(40, 20)) as pilot:
        log = app.query_one(Transcript)
        await _written(pilot, log)
        assert log.scroll_y == log.max_scroll_y
        app.query_one("#box").display = True
        await pilot.pause()
        await pilot.pause()
        assert log.size.height < 20
        assert log.scroll_y == log.max_scroll_y  # "line 39" still shows


async def test_where_you_scrolled_to_stays_when_it_gets_shorter():
    app = Pane()
    async with app.run_test(size=(40, 20)) as pilot:
        log = app.query_one(Transcript)
        await _written(pilot, log)
        log.scroll_to(y=3, animate=False)
        await pilot.pause()
        app.query_one("#box").display = True
        await pilot.pause()
        await pilot.pause()
        assert log.scroll_y == 3
