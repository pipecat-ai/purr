"""The Textual app: hosts the Purr process and handles navigation."""

import asyncio
import os
import shlex
from datetime import UTC, datetime
from pathlib import Path

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.screen import ModalScreen

from purr.app import Purr
from purr.checkout import github_repos
from purr.messages import (
    ActionPending,
    ActionResolved,
    PermissionRequest,
    PermissionResolved,
    PRChanged,
    PRListUpdated,
    ReviewFailed,
    ReviewFinished,
    ReviewProgress,
    ReviewStarted,
    SessionMessage,
)
from purr.models import PRChange, PRRef
from purr.tui.bridge import BusEvent, RequestError, UIWorker
from purr.tui.recap import RecapScreen
from purr.tui.screens import ConfirmScreen, MainScreen
from purr.tui.session import SessionState
from purr.tui.switcher import PurrSwitcher, short_name
from purr.workers.recap import LAST_ACTIVE

_NOWHERE = object()


class PurrApp(App):
    """Purr's TUI.

    Runs the Purr process (store, runner, workers) on Textual's event loop, with
    a :class:`UIWorker` as its bus participant. Views draw from the store and
    redraw when bus events say something changed.

    Navigation: the repo list is home; ``enter``/``1``–``9`` open a repo,
    ``esc``/``0`` go home, ``ctrl+o`` goes back to where you were before.

    Args:
        purr: The (not yet started) Purr process to host.
        cwd: Directory Purr was launched from; if it's a checkout of a watched
            repo, that repo opens directly.
    """

    CSS_PATH = "purr.tcss"
    TITLE = "Purr"
    COMMANDS = {PurrSwitcher}
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+o", "back", "Back", show=False, priority=True),
        Binding("ctrl+n", "next_alert", "Next alert", show=False, priority=True),
        Binding("w", "recap", "Recap", show=False),
        Binding("i", "instructions", "Review style"),
        Binding("0", "home", "Repos", show=False),
        *[Binding(str(n), f"jump({n})", show=False) for n in range(1, 10)],
    ]

    def __init__(self, purr: Purr, *, cwd: Path | None = None):
        super().__init__()
        self.purr = purr
        self.bus = UIWorker(self)
        self.main = MainScreen()
        self.repo_names: list[str] = []  # the repo list's order, for 1-9
        self.location: str | None = None  # the open repo, or None for the repo list
        self._previous: object = _NOWHERE
        self._cwd = cwd
        self.data_version = 0  # bumped whenever the store's data changes
        self.sessions: dict[str, SessionState] = {}  # per PR, see SessionState
        self.alerts: dict[str, str] = {}  # PR -> what happened, newest last
        self._reload_scheduled = False
        self._stopping = False
        self._last_active: datetime | None = None  # last key or voice turn
        self._last_reported: datetime | None = None

    async def on_mount(self) -> None:
        await self.purr.start(self.bus)
        # When the user was last around, including in an earlier run.
        if last := await self.purr.store.get_meta(LAST_ACTIVE):
            self._last_active = datetime.fromisoformat(last)
        await self.push_screen(self.main)
        await self.main.show_home()
        if self._cwd:
            self.run_worker(self._open_checkout_repo(self._cwd))

    async def _open_checkout_repo(self, cwd: Path) -> None:
        candidates = await github_repos(cwd)
        if not candidates:
            return
        watched = {r.name.lower(): r.name for r in await self.purr.store.list_repos()}
        for candidate in candidates:
            if candidate.lower() in watched:
                await self.open_repo(watched[candidate.lower()])
                return
        target = candidates[0]
        question = f"You're in a checkout of [b]{target}[/b]. Watch it?"
        if not await self.push_screen_wait(ConfirmScreen(question)):
            return
        try:
            name = await self.bus.add_repo(target)
        except RequestError as e:
            self.notify(str(e), severity="warning")
            return
        await self.open_repo(name)

    # -- Away and back ----------------------------------------------------------

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.Key):
            self._touch()
        await super().on_event(event)

    def _touch(self) -> None:
        now = datetime.now(UTC)
        away_after = self.purr.config.away_after
        last = self._last_active
        if last is None:
            last = self._last_active = now  # first run ever
        if (now - last).total_seconds() > away_after and not self._modal_open():
            self.push_screen(RecapScreen(since=last.isoformat(timespec="seconds")))
        self._last_active = now
        if self._last_reported is None or (now - self._last_reported).total_seconds() > 60:
            self._last_reported = now
            self.run_worker(self.bus.active())

    async def action_instructions(self) -> None:
        """Edit how you like reviews: this repo's file inside a repo, the general one at home."""
        if self._modal_open():
            return
        instructions = self.purr.instructions
        if self.location:
            path = instructions.ensure(instructions.repo_path(self.location), repo=self.location)
        else:
            path = instructions.ensure(instructions.global_path())
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        with self.suspend():
            process = await asyncio.create_subprocess_exec(*editor, str(path))
            await process.wait()
        self.notify(f"Saved {path}. It applies from the agents' next turn.")

    async def action_recap(self) -> None:
        if self._modal_open():
            return
        if recap := await self.purr.store.latest_recap():
            await self.push_screen(RecapScreen(recap=recap))
        else:
            self.notify("No recaps yet.")

    # -- Navigation -------------------------------------------------------------

    def _modal_open(self) -> bool:
        return isinstance(self.screen, ModalScreen)

    async def open_repo(self, name: str, *, remember: bool = True) -> None:
        if self.location == name:
            return
        if remember:
            self._previous = self.location
        self.location = name
        await self.main.show_repo(name)

    async def go_home(self, *, remember: bool = True) -> None:
        if self.location is None:
            return
        if remember:
            self._previous = self.location
        self.location = None
        await self.main.show_home()

    async def action_home(self) -> None:
        if not self._modal_open():
            await self.go_home()

    async def action_jump(self, number: int) -> None:
        if not self._modal_open() and number <= len(self.repo_names):
            await self.open_repo(self.repo_names[number - 1])

    async def goto_pr(self, ref: PRRef) -> None:
        """Open a PR's repo with that PR selected."""
        self.main.repo.select(str(ref), repo=ref.repo)
        await self.open_repo(ref.repo)

    async def action_next_alert(self) -> None:
        if self._modal_open() or not self.alerts:
            return
        pr = next(reversed(self.alerts))
        await self.goto_pr(PRRef.parse(pr))

    def seen(self, pr: str) -> None:
        """The user is looking at a PR: its alert is done."""
        if self.alerts.pop(pr, None) is not None:
            self._alerts_changed()

    def _alert(self, pr: str, text: str) -> None:
        if PRRef.parse(pr).repo == self.location:
            return  # it's on screen already
        self.alerts.pop(pr, None)
        self.alerts[pr] = text
        self._alerts_changed()

    def _alerts_changed(self) -> None:
        self.main.set_alert(self._alert_text())
        if self.location is None:
            self.data_version += 1  # the repo list's ● markers
            self.run_worker(self._reload())

    def _alert_text(self) -> str:
        if not self.alerts:
            return ""
        pr, text = next(reversed(self.alerts.items()))
        return f"🔔 {len(self.alerts)} · {short_name(PRRef.parse(pr))} {text}  (ctrl+n)"

    async def action_back(self) -> None:
        if self._modal_open() or self._previous is _NOWHERE:
            return
        target = self._previous
        if target is None:
            await self.go_home()
        else:
            await self.open_repo(str(target))

    # -- Live updates -------------------------------------------------------------

    def on_bus_event(self, message: BusEvent) -> None:
        event = message.event
        if isinstance(event, PRChanged) and event.change == PRChange.ASSIGNED:
            self._alert(event.pr, "assigned to you")
        elif isinstance(event, ReviewFinished):
            self._alert(event.pr, "review done")
        elif isinstance(event, ReviewFailed):
            self._alert(event.pr, "review failed")
        elif isinstance(event, PermissionRequest):
            self._alert(event.pr, "needs your OK")
        if isinstance(event, (PRListUpdated, PRChanged)):
            self.data_version += 1
            if not self._reload_scheduled:
                # Coalesce bursts (a poll can send many events) into one redraw.
                self._reload_scheduled = True
                self.set_timer(0.05, self._reload)
            return
        pr = getattr(event, "pr", None)
        if pr is None:
            return
        session = self.main.repo.session
        state = self.sessions.setdefault(pr, SessionState())
        if isinstance(event, SessionMessage):
            session.append(pr, event.message)
            return
        if isinstance(event, ReviewStarted):
            state.entries, state.progress, state.ask = [], None, None  # a new session
        elif isinstance(event, ReviewProgress):
            state.progress = event.text
        elif isinstance(event, (ReviewFinished, ReviewFailed)):
            state.progress = None
        elif isinstance(event, PermissionRequest):
            state.ask = event
        elif isinstance(event, PermissionResolved):
            state.ask = None
        elif not isinstance(event, (ActionPending, ActionResolved)):
            return
        if session.pr and str(session.pr.ref) == pr:
            if isinstance(event, ReviewStarted):
                self.run_worker(session.show(session.pr), group="session-status")
            else:
                self.run_worker(session.refresh_status(), group="session-status")

    async def _reload(self) -> None:
        self._reload_scheduled = False
        await self.main.current.reload()

    # -- Shutdown -------------------------------------------------------------------

    async def action_quit(self) -> None:
        await self._stop_purr()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_purr()

    async def _stop_purr(self) -> None:
        if not self._stopping:
            self._stopping = True
            await self.purr.stop()
