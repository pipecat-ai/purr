"""The prompt (the ask box, and the comment editor): Emacs keys."""

from textual.app import App, ComposeResult

from purr.tui.prompt import PromptArea


class OneBox(App):
    ENABLE_COMMAND_PALETTE = False  # Purr's own app gives ctrl+p to the box (TAKES_OVER)

    def compose(self) -> ComposeResult:
        yield PromptArea(id="box")


async def test_emacs_keys_move_kill_and_yank():
    app = OneBox()
    async with app.run_test() as pilot:
        box = app.query_one(PromptArea)
        box.focus()
        await pilot.press(*"one two three")
        await pilot.press("ctrl+a", "alt+f", "alt+b")  # a word forward, and back
        assert box.cursor_location == (0, 0)
        await pilot.press("alt+d")  # kill a word forward...
        assert box.text == " two three"
        await pilot.press("ctrl+e", "ctrl+y")  # ...and yank it back elsewhere
        assert box.text == " two threeone"
        await pilot.press("alt+backspace", "ctrl+a", "ctrl+y")  # back a word, the same
        assert box.text == "threeone two "  # ctrl+a: the very start, blanks or not

        # ctrl+k kills to the end of the line; at its end, the line break.
        box.set_text("first\nsecond")
        box.move_cursor((0, 2))
        await pilot.press("ctrl+k")
        assert box.text == "fi\nsecond"
        await pilot.press("ctrl+k")
        assert box.text == "fisecond"
        await pilot.press("alt+greater_than_sign")  # the end of the text, and its start
        assert box.cursor_location == (0, 8)
        await pilot.press("alt+less_than_sign")
        assert box.cursor_location == (0, 0)


async def test_ctrl_p_and_n_go_through_what_you_asked():
    app = OneBox()
    async with app.run_test() as pilot:
        box = app.query_one(PromptArea)
        box.focus()
        box.set_history(["is it safe?", "tests?"])
        await pilot.press("ctrl+p")
        assert box.text == "tests?"
        await pilot.press("ctrl+p")
        assert box.text == "is it safe?"
        await pilot.press("ctrl+n", "ctrl+n")
        assert box.text == ""
