"""The Textual app: hosts the Purr process and handles navigation."""

import asyncio
import os
import time
from functools import partial
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.worker import NoActiveWorker, get_current_worker

from purr.app import Purr
from purr.checkout import github_repos
from purr.config import SETTINGS_FILE, parse_settings, save_setting, settings_template
from purr.jobs import PurrError
from purr.messages import (
    REVIEW_WORKER,
    ActionPending,
    ActionResolved,
    ChooseReviewSkill,
    CommentsChanged,
    ListReviewSkills,
    MarkSeen,
    PermissionRequest,
    PermissionResolved,
    PRChanged,
    PRListUpdated,
    PurrEvent,
    ReviewFailed,
    ReviewFinished,
    ReviewProgress,
    ReviewStarted,
    ReviewStopped,
    SessionMessage,
    SkillChosen,
    SkillQuestion,
    WatchRepo,
    payload,
)
from purr.models import PRRef
from purr.news import news
from purr.review.instructions import Kind
from purr.review.rules import CLAUDE_SETTINGS
from purr.review.skills import RepoSkill, Skill
from purr.tui import browser, theme
from purr.tui.bridge import BusEvent, UIWorker
from purr.tui.clipboard import copy_command, system_copy, terminal_copies
from purr.tui.dialogs import (
    ConfirmScreen,
    ReviewSkillScreen,
    Sections,
    SettingItem,
    SettingsScreen,
    ThemeScreen,
)
from purr.tui.editor import EditorScreen, check_json, shown
from purr.tui.keyhelp import KeysScreen, key_groups
from purr.tui.overview import DetailsCache
from purr.tui.screens import MainScreen
from purr.tui.session import SessionState
from purr.tui.switcher import PurrSwitcher, short_name
from purr.tui.working import Working

_NOWHERE = object()
# The app's keys that the screen on top (a dialog, the diff) gets instead.
_GIVEN_WAY = {"next_alert", "command_palette"}
_PERMISSIONS = "What the agents may do without asking: your Claude Code settings"
# The settings menu: each entry's letter, name and what it's for (see _settings_menu).
_MENU = {
    "review": ("r", "Review style", "How reviews should go: what to raise, what to skip, tone"),
    "code": ("c", "Code style", "What good code looks like to you; reviews hold PRs to it"),
    "permissions": ("p", "Permissions", "What the agents may run without asking"),
    "skill": ("s", "Review skill", "Which of the repo's own skills its reviews follow"),
    "settings": ("o", "Settings", "Models, polling, reviews at once, sandbox…"),
    "theme": ("t", "Theme", "Purr's colors, each shown as you pick"),
}
# What each of your instruction files is for, over the editor.
_ABOUT = {
    Kind.REVIEW: "How you like reviews: what to raise, what to skip, tone",
    Kind.CODE: "What good code looks like to you: reviews hold PRs to it",
}


def _claude_settings() -> Path:
    """Your Claude Code settings file, made (empty) if you don't have one yet."""
    if not CLAUDE_SETTINGS.exists():
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text("{}\n")
    return CLAUDE_SETTINGS


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
        Binding("ctrl+q", "request_quit", "Quit", show=False, priority=True),  # q, from anywhere
        Binding("ctrl+o", "back", "Back", show=False, priority=True),
        # The keys that work everywhere: ? lists them; the footer has the place's own.
        Binding("ctrl+p", "command_palette", "Find", show=False, priority=True),  # repo, PR
        Binding("ctrl+n", "next_alert", "News", show=False, priority=True),  # the latest
        Binding("i", "instructions", "Settings", show=False),
        Binding("0", "home", "Home", show=False),
        *[Binding(str(n), f"jump({n})", "Repo 1–9", show=False) for n in range(1, 10)],
        Binding("q", "request_quit", "Quit", show=False),
        Binding("question_mark", "keys", "Keys", show=False),  # each page shows its own
    ]

    def __init__(self, purr: Purr, *, cwd: Path | None = None):
        super().__init__()
        self._use_theme(purr.config.theme)
        self.purr = purr
        self.bus = UIWorker(self)
        self.main = MainScreen()
        self.repo_names: list[str] = []  # the repo list's order, for 1-9
        self.location: str | None = None  # the open repo, or None for the repo list
        self._previous: object = _NOWHERE
        self._cwd = cwd
        self.data_version = 0  # bumped whenever the store's data changes
        self.sessions: dict[str, SessionState] = {}  # per PR, see SessionState
        self.full_thinking = False  # the agents' reasoning in full, or a line per thought (t)
        self.details = DetailsCache()  # PR descriptions, files and CI
        self.repo_skills: dict[str, list[RepoSkill]] = {}  # for / commands, once per repo
        # PR -> what happened since you last looked at it, newest last (from
        # the store on startup, then live).
        self.alerts: dict[str, str] = {}
        self._needs_you: dict[str, int] = {}  # repo -> PRs needing you there
        self._reload_scheduled = False
        self._stopping = False

    def _use_theme(self, name: str) -> None:
        """Start in the theme (settings): it, what the named colors are in it, Purr's own."""
        self.theme = name
        dark, light = theme.ansi_themes(name)
        self.set_reactive(PurrApp.ansi_theme_dark, dark)
        self.set_reactive(PurrApp.ansi_theme_light, light)
        theme.use(name, self.get_css_variables(), dark=self.current_theme.dark)

    async def change_theme(self, name: str) -> None:
        """Another theme, now: the widgets and the named colors take it at once; Purr's own
        colors are in the rows and the transcript as they were drawn, so those are drawn
        again (``restyle``)."""
        if name == self.theme:
            return
        self.theme = name
        self.ansi_theme_dark, self.ansi_theme_light = theme.ansi_themes(name)
        theme.use(name, self.get_css_variables(), dark=self.current_theme.dark)
        await self.main.repos.reload()
        await self.main.repo.restyle()

    def choose_theme(self) -> None:
        """/theme: the theme picker (see _theme_picker), unless a dialog's open."""
        if not self._modal_open():
            self._theme_picker()

    def _theme_picker(self) -> None:
        """The themes, each shown as you move to it: enter keeps it (in settings.toml), esc
        goes back to the one you had."""
        was = self.theme

        async def chosen(name: str | None) -> None:
            await (self.change_theme(was) if name is None else self.keep_theme(name))

        self.push_screen(ThemeScreen(theme.theme_names(), was), chosen)

    async def keep_theme(self, name: str) -> None:
        """This theme, now, and from now on (settings.toml)."""
        await self.change_theme(name)
        path = self.purr.config.config_dir / SETTINGS_FILE
        save_setting(path, "theme", name)
        self.notify(f"Theme: {name} (saved in {path})")

    async def on_mount(self) -> None:
        await self.purr.start(self.bus)
        # What's happened since you last looked, including before this run.
        self.alerts = {str(ref): what for ref, what in await self.purr.store.unseen()}
        await self.push_screen(self.main)
        await self.main.show_home()
        await self._count_needs_you()
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
            name = (await self.bus.request(WatchRepo(repo=target))).repo
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        await self.open_repo(name)

    async def action_instructions(self) -> None:
        """i: the settings menu: how reviews go (your review and code style, the agents'
        permissions: this repo's inside a repo, and there which of its skills its reviews
        follow; the general ones at home), and Purr's settings and theme. What it opens
        goes over it, and closing that comes back to it."""
        if self._modal_open():
            return
        repo = self.location
        menu = SettingsScreen(lambda: self._settings_menu(repo), partial(self._setting, repo=repo))
        await self.push_screen(menu)

    async def _setting(self, kind: str, *, repo: str | None) -> None:
        """Open one of the settings menu's (over it)."""
        if kind == "skill" and repo:
            await self._choose_review_skill(repo)
        elif kind == "permissions":
            about = f"What agents may do in {repo}'s reviews" if repo else _PERMISSIONS
            path = self.purr.rules.ensure(repo) if repo else _claude_settings()
            self._edit(path, about=about, check=check_json)
        elif kind == "settings":
            self._edit_settings()
        elif kind == "theme":
            self._theme_picker()
        elif kind in (Kind.REVIEW, Kind.CODE):
            path = self.purr.instructions.ensure(Kind(kind), repo)
            self._edit(path, about=_ABOUT[Kind(kind)])

    def _settings_menu(self, repo: str | None) -> Sections:
        """What the settings menu lists (see _MENU), with where each one is."""
        files = {
            "review": self.purr.instructions.path(Kind.REVIEW, repo),
            "code": self.purr.instructions.path(Kind.CODE, repo),
            "permissions": self.purr.rules.path(repo) if repo else CLAUDE_SETTINGS,
            "settings": self.purr.config.config_dir / SETTINGS_FILE,
        }

        def item(kind: str) -> SettingItem:
            where = self.theme if kind == "theme" else shown(files[kind]) if kind in files else ""
            return SettingItem(kind, *_MENU[kind], where=where)

        yours = ["review", "code", "permissions", *(["skill"] if repo else [])]
        return [
            (f"Reviews, in {repo}" if repo else "Reviews, in every repo", [item(k) for k in yours]),
            ("Purr", [item("settings"), item("theme")]),
        ]

    async def _choose_review_skill(self, repo: str) -> None:
        try:
            found = await self.bus.request(ListReviewSkills(repo=repo))
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        if not found.skills:
            self.notify(
                f"None of {repo}'s skills look like they're for reviewing. You can still "
                "run any of them in a PR's session, as /<plugin>:<skill>."
            )
            return
        screen = ReviewSkillScreen(repo, found.skills, found.chosen)
        self.push_screen(screen, self._set_review_skill(repo))

    def _set_review_skill(self, repo: str):
        async def answer(skill: str | None) -> None:
            try:
                await self.bus.request(ChooseReviewSkill(repo=repo, skill=skill))
            except PurrError as e:
                self.notify(str(e), severity="warning")

        return answer

    def browse(self, url: str, what: str) -> None:
        """Open ``what`` (``#123``, a repo) on GitHub, in your browser."""
        try:
            browser.open_page(url)
        except OSError as e:
            self.notify(f"Couldn't open your browser: {e}", severity="warning")
            return
        self.notify(f"Opening {what} on GitHub")

    async def run_outside(
        self, command: list[str], cwd: str, env: dict[str, str] | None = None, *, banner: str = ""
    ) -> int | None:
        """Hand the terminal to another program (Claude Code, a shell) until it exits, after a
        line saying where you are and how to come back (``banner``). Its exit status; None if
        it couldn't be started (not there).

        Purr keeps running underneath: polls, reviews.
        """
        with self.suspend():
            if banner:
                print(banner, flush=True)
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=cwd, env={**os.environ, **(env or {})}
                )
            except OSError:
                return None
            return await process.wait()

    def _edit(self, path: Path, **kwargs) -> None:
        """A file of yours, in Purr's editor (see :class:`EditorScreen` for the rest)."""
        self.push_screen(EditorScreen(path, **kwargs))

    def _edit_settings(self) -> None:
        """Purr's settings.toml, made with every setting commented out if it's not there yet.
        It's checked before it's saved, so a typo shows at once; a new theme applies then."""
        path = self.purr.config.config_dir / SETTINGS_FILE
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(settings_template())

        async def saved(text: str) -> None:
            await self.change_theme(parse_settings(text).get("theme", theme.DEFAULT))

        self._edit(
            path,
            about="Purr's settings: models, polling, sandbox, theme…",
            applies="next time Purr starts (the theme, now)",
            check=parse_settings,
            saved=saved,
        )

    # -- Navigation -------------------------------------------------------------

    def action_keys(self) -> None:
        """? : every key that works where you are (see purr.tui.keyhelp)."""
        if not self._modal_open():
            self.push_screen(KeysScreen(key_groups(self)))

    def _modal_open(self) -> bool:
        return isinstance(self.screen, ModalScreen)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Over a dialog, ctrl+n and ctrl+p are its own; and a widget can take any of the
        # app's keys while it has the focus (TAKES_OVER: the diff moves with ctrl+n/p).
        if action in _GIVEN_WAY and self._modal_open():
            return False
        if action in getattr(self.focused, "TAKES_OVER", ()):
            return False
        return super().check_action(action, parameters)

    async def open_repo(self, name: str, *, remember: bool = True) -> None:
        if self.location == name:
            return
        if remember:
            self._previous = self.location
        self.location = name
        await self.main.show_repo(name)
        self.main.set_alert(self._alert_text())

    async def go_home(self, *, remember: bool = True) -> None:
        if self.location is None:
            return
        if remember:
            self._previous = self.location
        self.location = None
        await self.main.show_home()
        self.main.set_alert(self._alert_text())

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
        """The newest news, from another repo if there's any."""
        if self._modal_open() or not self.alerts:
            return
        elsewhere = self._alerts_elsewhere()
        pr = elsewhere[-1] if elsewhere else next(reversed(self.alerts))
        await self.goto_pr(PRRef.parse(pr))

    def seen(self, pr: str) -> None:
        """The user is looking at a PR: what happened there isn't news anymore."""
        if self.alerts.pop(pr, None) is not None:
            self.run_worker(self.bus.request(MarkSeen(pr=PRRef.parse(pr))), group="seen")
            self._alerts_changed()

    def _alert(self, pr: str, text: str) -> None:
        shown = self.main.repo.session.pr if self.main.is_mounted else None
        if self.location and shown and str(shown.ref) == pr:
            self.run_worker(self.bus.request(MarkSeen(pr=PRRef.parse(pr))), group="seen")
            return  # you're looking at it
        self.alerts.pop(pr, None)
        self.alerts[pr] = text
        self._alerts_changed()

    def _alerts_changed(self) -> None:
        self.main.set_alert(self._alert_text())
        # The repo list's ● markers, the PR list's bold titles.
        self.data_version += 1
        self._start_reload()

    def _alerts_elsewhere(self) -> list[str]:
        return [pr for pr in self.alerts if PRRef.parse(pr).repo != self.location]

    def _alert_text(self) -> str:
        """One thing about other repos, so you know to go and look: news first."""
        if elsewhere := self._alerts_elsewhere():
            pr = elsewhere[-1]
            what = self.alerts[pr]
            return f"🔔 {len(elsewhere)} · {short_name(PRRef.parse(pr))} {what}  (ctrl+n)"
        if self.location is None:
            return ""  # the repo list shows what needs you
        needs = sum(n for repo, n in self._needs_you.items() if repo != self.location)
        return f"⚠ {needs} need{'s' if needs == 1 else ''} you in other repos  (0)" if needs else ""

    async def _count_needs_you(self) -> None:
        summaries = await self.purr.store.repo_summaries()
        self._needs_you = {summary.name: summary.needs_you for summary in summaries}
        self.main.set_alert(self._alert_text())

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
        if self._shutting_down:
            return  # nothing left to show it on, and the store may be closed
        if handle := self._EVENT_HANDLERS.get(type(message.event)):
            handle(self, message.event)

    @property
    def _shutting_down(self) -> bool:
        """Purr is stopping, or Textual is taking the app down: the store and screens go."""
        return self._stopping or not self.is_running

    def _ask_review_skill(self, event: SkillQuestion) -> None:
        skills = [Skill(**skill) for skill in event.skills]
        screen = ReviewSkillScreen(event.repo, skills, asking=True)
        self.push_screen(screen, self._set_review_skill(event.repo))

    def _review_skill_chosen(self, event: SkillChosen) -> None:
        if event.chosen_by != "purr":
            return
        self.notify(
            f"Reviews in {event.repo} follow its /{event.skill} skill. "
            "To change that, press i in the repo.",
            timeout=12,
        )

    def _prs_updated(self, event: PRListUpdated) -> None:
        self._schedule_reload()

    def _pr_changed(self, event: PRChanged) -> None:
        self._tell(event)
        self._schedule_reload()

    def _tell(self, event: PurrEvent) -> None:
        """An event that's news to you (see purr.news): an alert, the PR's title in bold."""
        if what := news(event.kind, payload(event)):
            self._alert(event.pr, what)

    def _session_message(self, event: SessionMessage) -> None:
        """A step in a PR's session: into its transcript, or how many tasks are out."""
        session = self.main.repo.session
        state = self._session_state(event.pr)
        if event.message.get("kind") != "background":
            session.append(event.pr, event.message)
            return
        state.background = int(event.message.get("running") or 0)
        if session.pr and str(session.pr.ref) == event.pr:
            session.query_one(Working).set_background(state.background)

    def _review_started(self, event: ReviewStarted) -> None:
        state = self._session_state(event.pr)
        state.entries, state.ask = [], None  # a new session
        state.progress, state.waiting, state.since = None, False, None
        state.background = 0
        self._session_changed(event.pr, new=True)

    def _review_progress(self, event: ReviewProgress) -> None:
        state = self._session_state(event.pr)
        state.progress, state.waiting = event.text or None, event.waiting
        if not state.progress:
            state.since, state.background = None, 0
        elif state.since is None:
            state.since = time.monotonic()  # the agent's working: the spinner's clock
        self._session_changed(event.pr)

    def _review_finished(self, event: ReviewFinished) -> None:
        self._tell(event)
        self._review_ended(event.pr)

    def _review_failed(self, event: ReviewFailed) -> None:
        self._tell(event)
        self._review_ended(event.pr)

    def _review_stopped(self, event: ReviewStopped) -> None:
        """You stopped it. A review that gave way to the one before: that one's conversation."""
        self._review_ended(event.pr)
        if event.dropped:
            state = self._session_state(event.pr)
            state.entries, state.ask = None, None  # the earlier review's, read again
            self._session_changed(event.pr, new=True)

    def _review_ended(self, pr: str) -> None:
        state = self._session_state(pr)
        state.progress, state.waiting, state.since = None, False, None
        state.background = 0
        self._session_changed(pr)

    def _permission_asked(self, event: PermissionRequest) -> None:
        self._tell(event)
        self._session_state(event.pr).ask = event
        self._session_changed(event.pr)

    def _permission_answered(self, event: PermissionResolved) -> None:
        self._session_state(event.pr).ask = None
        self._session_changed(event.pr)

    def _action_changed(self, event: ActionPending | ActionResolved | CommentsChanged) -> None:
        """A proposal waits for you, or was settled, or your comments changed: the decision
        box changes, and the diff's comments."""
        self.main.repo.diff_changed(event.pr)
        self._session_changed(event.pr)

    def _session_state(self, pr: str) -> SessionState:
        return self.sessions.setdefault(pr, SessionState())

    def _session_changed(self, pr: str, *, new: bool = False) -> None:
        """A PR's review moved on: its mark in the list, and its pane if it's the one shown
        (all of it for a new review, else what the header and decision box say)."""
        self.main.repo.working_changed()
        session = self.main.repo.session
        if not session.pr or str(session.pr.ref) != pr:
            return
        update = session.show(session.pr) if new else session.refresh_status()
        self.run_worker(update, group="session-status")

    # What each bus event does here. A new event: its handler, and a line here.
    _EVENT_HANDLERS = {
        SkillQuestion: _ask_review_skill,
        SkillChosen: _review_skill_chosen,
        PRListUpdated: _prs_updated,
        PRChanged: _pr_changed,
        SessionMessage: _session_message,
        ReviewStarted: _review_started,
        ReviewProgress: _review_progress,
        ReviewFinished: _review_finished,
        ReviewFailed: _review_failed,
        ReviewStopped: _review_stopped,
        PermissionRequest: _permission_asked,
        PermissionResolved: _permission_answered,
        ActionPending: _action_changed,
        ActionResolved: _action_changed,
        CommentsChanged: _action_changed,
    }

    def _schedule_reload(self) -> None:
        self.data_version += 1
        if not self._reload_scheduled:
            # Coalesce bursts (a poll can send many events) into one redraw.
            self._reload_scheduled = True
            self.set_timer(0.05, self._start_reload)

    def _start_reload(self) -> None:
        """Redraw the view from the store, in a worker, the latest reload only.

        A worker, not the app's own message loop: quitting cancels and waits for
        workers before closing the store, while nothing waits for that loop (Textual
        under test doesn't either), so a reload there could read a closed store.
        """
        self._reload_scheduled = False
        # The method, not a coroutine: one cancelled before it starts leaves none behind.
        self.run_worker(self._reload, group="reload", exclusive=True)

    async def _reload(self) -> None:
        if self._shutting_down:
            # Its timer can escape Textual stopping the app's timers (when set while
            # they're being stopped), and Textual runs what it scheduled right after
            # on_unmount, when the screens are gone and the store's closed.
            return
        await self.main.current.reload()
        await self._count_needs_you()

    # -- Shutdown -------------------------------------------------------------------

    @work(group="quit")
    async def action_request_quit(self) -> None:
        """q or ctrl+q: quit, once you say so."""
        if self._modal_open():
            return
        running = len(getattr(self.purr.runner.get_worker(REVIEW_WORKER), "running", ()))
        detail = (
            f"{running} review{'s are' if running != 1 else ' is'} running; "
            "they pick up where they left off next time."
            if running
            else ""
        )
        if await self.push_screen_wait(ConfirmScreen("Quit Purr?", detail)):
            await self.action_quit()

    def on_text_selected(self) -> None:
        """Copy what you select with the mouse, as terminals do."""
        if text := self.screen.get_selected_text():
            self.copy_to_clipboard(text)

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)  # by asking the terminal, which works over ssh too
        command = copy_command()
        if command:
            self.run_worker(system_copy(command, text))
        elif not terminal_copies():
            self.notify(
                "This terminal doesn't let apps copy. Install wl-clipboard (or xclip) and "
                "Purr will; until then, hold shift to select with the terminal itself.",
                severity="warning",
                timeout=8,
            )
            return
        lines = text.count("\n") + 1
        self.notify(f"Copied {lines} line{'s' if lines != 1 else ''}", timeout=2)

    async def action_quit(self) -> None:
        await self._stop_purr()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_purr()

    async def _stop_purr(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        # Nothing on screen needs finishing, and a worker still reading the store
        # (a PR's details, a repo opening) would fail once it's closed.
        await self._cancel_other_workers()
        await self.purr.stop()

    async def _cancel_other_workers(self) -> None:
        """Cancel every worker but the one this runs in (quitting runs in one).

        And wait until they've stopped: cancelling only asks, and a query cut short
        still closes its cursor in the store on the way out.
        """
        try:
            current = get_current_worker()
        except NoActiveWorker:
            current = None
        others = [worker for worker in self.workers if worker is not current]
        for worker in others:
            worker.cancel()
        await asyncio.gather(*(worker.wait() for worker in others), return_exceptions=True)
