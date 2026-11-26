"""What the main screen's two views share: home (the repos) and a repo's PRs."""

from typing import TYPE_CHECKING, cast

from textual.widget import Widget

if TYPE_CHECKING:
    from purr.tui.app import PurrApp


class PurrView(Widget):
    """A view in the main screen's switcher."""

    _drawn: tuple | None = None  # what the view last drew, to skip redundant redraws

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def still_here(self) -> bool:
        """Whether it's still on screen, with Purr running. After waiting on something (the
        store, GitHub, a timer), Purr may have quit meanwhile and its widgets be gone."""
        return self.is_attached and self.app.is_running

    def _key(self) -> tuple:
        return (self.purr_app.data_version,)

    async def refresh_if_stale(self) -> None:
        """Redraw only if the data changed since the last draw."""
        if self._drawn != self._key():
            await self.reload()

    async def reload(self) -> None:
        """Redraw from the store."""
        self._drawn = self._key()
