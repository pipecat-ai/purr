"""The quick switcher (ctrl+p): jump to any repo or PR by fuzzy search."""

from functools import partial
from typing import TYPE_CHECKING, cast

from textual.command import DiscoveryHit, Hit, Hits, Provider

from purr.models import PR, PRRef, PRState

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_STATE_LABEL = {
    PRState.NEEDS_YOU: "needs you",
    PRState.REVIEWING: "reviewing",
    PRState.NOT_REVIEWED: "not reviewed",
    PRState.DONE: "done",
}


def short_name(ref: PRRef) -> str:
    """``whisker#112``: the repo name without its owner."""
    return f"{ref.repo.split('/')[-1]}#{ref.number}"


class PurrSwitcher(Provider):
    """Repos and open PRs, needs-you first when nothing is typed yet."""

    async def startup(self) -> None:
        app = cast("PurrApp", self.app)
        self._repos = list(app.repo_names)
        self._prs: list[PR] = await app.purr.store.list_prs(open_only=True)

    def _go_repo(self, repo: str) -> None:
        app = cast("PurrApp", self.app)
        app.run_worker(app.open_repo(repo))

    def _go_pr(self, ref: PRRef) -> None:
        app = cast("PurrApp", self.app)
        app.run_worker(app.goto_pr(ref))

    async def discover(self) -> Hits:
        for pr in self._prs:
            if pr.state == PRState.NEEDS_YOU:
                yield DiscoveryHit(
                    f"{short_name(pr.ref)}  {pr.title}",
                    partial(self._go_pr, pr.ref),
                    help="needs you",
                )
        for repo in self._repos:
            yield DiscoveryHit(repo, partial(self._go_repo, repo), help="repo")

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for repo in self._repos:
            if (score := matcher.match(repo)) > 0:
                yield Hit(score, matcher.highlight(repo), partial(self._go_repo, repo), help="repo")
        for pr in self._prs:
            text = f"{short_name(pr.ref)}  {pr.title}  {pr.author}"
            if (score := matcher.match(text)) > 0:
                yield Hit(
                    score,
                    matcher.highlight(text),
                    partial(self._go_pr, pr.ref),
                    help=_STATE_LABEL[pr.state],
                )
