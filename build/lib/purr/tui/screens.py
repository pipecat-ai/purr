"""Purr's main screen.

It holds both views, home (``home.py``) and a repo's PRs (``repo.py``), in a content
switcher. Both are built at startup, so moving between them only swaps which one is
visible and refreshes its data: no screen gets composed on the way. Dialogs
(``dialogs.py``) are modal screens on top.
"""

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import ContentSwitcher, Static

from purr.tui.home import ReposView
from purr.tui.keybar import KeyBar
from purr.tui.repo import RepoView
from purr.tui.views import PurrView

if TYPE_CHECKING:
    from purr.tui.app import PurrApp


class MainScreen(Screen):
    """Top bar, the current view (repo list or a repo), and the footer."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("Purr", id="crumb")
            yield Static(id="alert")
        with ContentSwitcher(initial="repos", id="views"):
            yield ReposView(id="repos")
            yield RepoView(id="repo")
        yield KeyBar()  # every key that works where you are

    @property
    def repos(self) -> ReposView:
        return self.query_one(ReposView)

    @property
    def repo(self) -> RepoView:
        return self.query_one(RepoView)

    def describe_key(self, binding: Binding) -> str:
        """What a key says it does (the footer, ?): most always say the same; a, in ?, what
        it does to the PR now (Post comment, Approve PR, Push…)."""
        if binding.action in ("approve", "repo('approve')"):
            return self.repo.a_does() or binding.description
        return binding.description

    @property
    def current(self) -> PurrView:
        return self.repos if self.query_one(ContentSwitcher).current == "repos" else self.repo

    def set_alert(self, text: str) -> None:
        # Events can arrive before the screen is up, and workers finish as it goes down
        # (mounted still, its widgets gone): then there's nothing to say it on.
        for alert in self.query("#alert").results(Static):
            alert.update(text)

    async def show_home(self) -> None:
        self.query_one("#crumb", Static).update("Purr")
        self.query_one(ContentSwitcher).current = "repos"
        self.repos.focus_main()
        await self.repos.refresh_if_stale()

    async def show_repo(self, name: str) -> None:
        app = cast("PurrApp", self.app)
        number = f"{app.repo_names.index(name) + 1} " if name in app.repo_names[:9] else ""
        self.query_one("#crumb", Static).update(f"Purr › {number}{name}")
        await self.repo.show(name)  # before it shows: never the last repo's list
        self.query_one(ContentSwitcher).current = "repo"
        self.repo.focus_main()
