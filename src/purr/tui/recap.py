"""The "While you were away" panel."""

from datetime import datetime
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from purr.models import PRRef, Recap
from purr.recap import event_text, is_notable
from purr.tui.bridge import RequestError
from purr.tui.switcher import short_name

if TYPE_CHECKING:
    from purr.tui.app import PurrApp


def _clock(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M")


class RecapScreen(ModalScreen[None]):
    """Shows a recap: the raw activity right away, the agent's summary when it's ready.

    Give it ``since`` to write a new recap, or ``recap`` to show one.
    """

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, *, since: str | None = None, recap: Recap | None = None):
        super().__init__()
        self._since = since or (recap.since if recap else None)
        self._recap = recap

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="recap"):
            yield Label("", id="recap-title")
            yield Static(Text("Summarizing…", style="dim"), id="recap-headline")
            yield OptionList(id="recap-items")
            with VerticalScroll(id="recap-raw"):
                yield Static(id="recap-events")
            yield Label("enter open PR · esc close", classes="hint")

    async def on_mount(self) -> None:
        until = self._recap.until if self._recap else None
        title = "While you were away"
        if self._since:
            title += f" · {_clock(self._since)} → {_clock(until) if until else 'now'}"
        self.query_one("#recap-title", Label).update(Text(title, style="bold"))
        await self._show_events()
        if self._recap:
            self._fill(self._recap)
        elif self._since:
            self._write(self._since)

    async def _show_events(self) -> None:
        store = self.purr_app.purr.store
        activities = [a for a in await store.activity(since=self._since) if is_notable(a)]
        lines = Text(f"{len(activities)} events\n", style="bold dim")
        for activity in activities:
            label = short_name(activity.pr) if activity.pr else ""
            lines.append(f"{_clock(activity.at)}  {label:<16} {event_text(activity)[:90]}\n", "dim")
        self.query_one("#recap-events", Static).update(lines)

    @work(exclusive=True)
    async def _write(self, since: str) -> None:
        try:
            recap_id = await self.purr_app.bus.create_recap(since)
        except RequestError as e:
            self.query_one("#recap-headline", Static).update(Text(str(e), style="red"))
            return
        recap = await self.purr_app.purr.store.get_recap(recap_id) if recap_id else None
        if recap is None:
            self.query_one("#recap-headline", Static).update("Nothing notable happened.")
            return
        self._recap = recap
        self._fill(recap)

    def _fill(self, recap: Recap) -> None:
        self.query_one("#recap-headline", Static).update(Text(recap.headline, style="bold"))
        options = []
        for item in recap.items:
            try:
                ref = PRRef.parse(str(item.get("pr", "")))
            except ValueError:
                continue
            text = Text("⚠ " if item.get("needs_you") else "  ", style="bold yellow")
            text.append(f"{short_name(ref):<16}", style="bold")
            text.append(str(item.get("text", "")))
            options.append(Option(text, id=str(ref)))
        items = self.query_one(OptionList)
        items.set_options(options)
        items.highlighted = 0 if options else None
        items.focus()

    @on(OptionList.OptionSelected)
    def _open(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            ref = PRRef.parse(event.option.id)
            self.dismiss(None)
            self.purr_app.run_worker(self.purr_app.goto_pr(ref))

    def action_close(self) -> None:
        self.dismiss(None)
