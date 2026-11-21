"""Read-only GitHub queries, through the GitHub CLI (``gh api graphql``).

Going through ``gh`` means Purr uses whatever GitHub login the user already has.
Everything here reads; writes (assigning, posting reviews, merging) live with
the code that executes approved actions.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field

from loguru import logger

from purr.models import PR, GHState, PRRef

# Searches are sorted by creation so that pages stay stable while PRs get updated.
POOL_QUERY = (
    "is:pr is:open archived:false draft:false no:assignee -author:@me sort:created-desc repo:{repo}"
)
ASSIGNED_QUERY = "is:pr is:open archived:false assignee:@me sort:created-desc"
# Every PR in a repo touched since a time, whatever its state: used between full
# syncs. Leaving the pool (assigned, closed, drafted) updates a PR too.
UPDATED_QUERY = "is:pr archived:false updated:>={since} sort:updated-desc repo:{repo}"

_PR_FIELDS = """
    number title url state isDraft updatedAt headRefOid
    author { login }
    assignees(first: 20) { nodes { login } }
    repository { nameWithOwner }
"""
_PAGE_SIZE = 100
_STATUS_BATCH = 50


class GitHubError(Exception):
    """GitHub couldn't be queried (gh missing, not logged in, network, ...)."""


@dataclass(frozen=True)
class RemotePR:
    """A PR as GitHub reports it."""

    repo: str
    number: int
    title: str
    author: str
    url: str
    head_sha: str
    updated_at: str
    is_draft: bool = False
    state: GHState = GHState.OPEN
    assignees: tuple[str, ...] = ()

    @property
    def ref(self) -> PRRef:
        return PRRef(self.repo, self.number)

    def in_pool(self, me: str) -> bool:
        """Open, ready for review, nobody assigned, and not yours."""
        return (
            self.state == GHState.OPEN
            and not self.is_draft
            and not self.assignees
            and self.author.lower() != me.lower()
        )

    def to_pr(self, *, assigned_to_me: bool) -> PR:
        return PR(
            repo=self.repo,
            number=self.number,
            title=self.title,
            author=self.author,
            url=self.url,
            head_sha=self.head_sha,
            updated_at=self.updated_at,
            assigned_to_me=assigned_to_me,
        )


@dataclass
class Snapshot:
    """One poll's view of GitHub.

    Parameters:
        pool: Fully synced repos: every PR in each repo's pool.
        assigned: Every open PR assigned to you, in any repo.
        updates: Incrementally synced repos: every PR touched since the last
            sync, whatever its state. Absence means "unchanged".
        viewer: Your GitHub login (needed to classify ``updates``).

    Only complete results are included: a repo whose search failed or came back
    partial is missing from ``pool``/``updates``, and ``assigned`` is None if
    that search did. Absence from an incomplete result says nothing about a PR.
    """

    pool: dict[str, list[RemotePR]]
    assigned: list[RemotePR] | None
    updates: dict[str, list[RemotePR]] = field(default_factory=dict)
    viewer: str | None = None


@dataclass(frozen=True)
class PRStatus:
    state: GHState
    head_sha: str


def _remote_pr(node: dict) -> RemotePR:
    return RemotePR(
        repo=node["repository"]["nameWithOwner"],
        number=node["number"],
        title=node["title"],
        author=(node.get("author") or {}).get("login", "ghost"),
        url=node["url"],
        head_sha=node["headRefOid"],
        updated_at=node["updatedAt"],
        is_draft=node["isDraft"],
        state=GHState(node.get("state", "OPEN").lower()),
        assignees=tuple(a["login"] for a in (node.get("assignees") or {}).get("nodes", [])),
    )


def _failed_aliases(body: dict) -> set[str]:
    return {str(error["path"][0]) for error in body.get("errors", []) if error.get("path")}


class GitHub:
    """Queries GitHub's GraphQL API via ``gh``.

    Args:
        gh: The GitHub CLI executable.
    """

    def __init__(self, gh: str = "gh"):
        self._gh = gh
        self._viewer: str | None = None

    async def graphql(self, query: str, variables: dict[str, str | int | None]) -> dict:
        """Run a GraphQL query and return the whole response body (data and errors).

        A partial failure (some fields errored) still returns the body; callers
        check ``errors`` per alias. Raises GitHubError when there's no usable body.
        """
        args = [self._gh, "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                continue
            args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError as e:
            raise GitHubError(
                f"GitHub CLI not found ({self._gh}); install it from cli.github.com"
            ) from e
        stdout, stderr = await process.communicate()
        try:
            body = json.loads(stdout)
        except json.JSONDecodeError:
            message = stderr.decode().strip() or f"gh exited with {process.returncode}"
            raise GitHubError(message) from None
        if not isinstance(body, dict) or body.get("data") is None:
            errors = body.get("errors", []) if isinstance(body, dict) else []
            raise GitHubError("; ".join(e.get("message", str(e)) for e in errors) or str(body))
        return body

    async def repo_name(self, name: str) -> str | None:
        """A repo's canonical ``owner/name``; None if it doesn't exist or you can't see it."""
        owner, _, repo = name.partition("/")
        body = await self.graphql(
            "query($owner: String!, $name: String!) {"
            " repository(owner: $owner, name: $name) { nameWithOwner } }",
            {"owner": owner, "name": repo},
        )
        repository = body["data"].get("repository")
        return repository["nameWithOwner"] if repository else None

    async def viewer_login(self) -> str:
        """Your GitHub login (looked up once)."""
        if self._viewer is None:
            body = await self.graphql("query { viewer { login } }", {})
            self._viewer = body["data"]["viewer"]["login"]
        return self._viewer

    async def snapshot(
        self,
        full: Iterable[str] = (),
        incremental: Mapping[str, str] | None = None,
        *,
        on_page: Callable[[str, list[RemotePR]], Awaitable[None]] | None = None,
    ) -> Snapshot:
        """Fetch watched repos and the PRs assigned to you, in as few requests as possible
        (one per page of results, all searches batched together).

        Args:
            full: Repos to fetch whole pools for.
            incremental: Repos to fetch only PRs updated since a time (``YYYY-MM-DDTHH:MM:SSZ``).
            on_page: Called with ``(repo, prs)`` for each page of a full pool as it
                arrives, so a newly added repo can show up before all pages are in.
        """
        incremental = dict(incremental or {})
        searches = {"assigned": ASSIGNED_QUERY}
        full_aliases: dict[str, str] = {}
        updated_aliases: dict[str, str] = {}
        for i, repo in enumerate(full):
            full_aliases[f"r{i}"] = repo
            searches[f"r{i}"] = POOL_QUERY.format(repo=repo)
        for i, (repo, since) in enumerate(incremental.items()):
            updated_aliases[f"u{i}"] = repo
            searches[f"u{i}"] = UPDATED_QUERY.format(repo=repo, since=since)

        async def page(alias: str, prs: list[RemotePR]) -> None:
            if on_page and alias in full_aliases:
                await on_page(full_aliases[alias], prs)

        viewer = await self.viewer_login() if incremental else None
        results = await self._search_all(searches, on_page=page)
        return Snapshot(
            pool={full_aliases[a]: prs for a, prs in results.items() if a in full_aliases},
            assigned=results.get("assigned"),
            updates={updated_aliases[a]: prs for a, prs in results.items() if a in updated_aliases},
            viewer=viewer,
        )

    async def _search_all(
        self,
        searches: dict[str, str],
        *,
        on_page: Callable[[str, list[RemotePR]], Awaitable[None]] | None = None,
    ) -> dict[str, list[RemotePR]]:
        """Run several searches, following pages. Returns only searches that completed."""
        found: dict[str, dict[int, RemotePR]] = {alias: {} for alias in searches}
        cursors: dict[str, str | None] = {alias: None for alias in searches}
        totals: dict[str, int] = {}
        failed: set[str] = set()

        while cursors:
            declared = ", ".join(f"$q_{a}: String!, $c_{a}: String" for a in cursors)
            fields = " ".join(
                f"{a}: search(query: $q_{a}, type: ISSUE, first: {_PAGE_SIZE}, after: $c_{a}) {{"
                f" issueCount pageInfo {{ hasNextPage endCursor }}"
                f" nodes {{ ... on PullRequest {{ {_PR_FIELDS} }} }} }}"
                for a in cursors
            )
            variables: dict[str, str | int | None] = {}
            for alias, cursor in cursors.items():
                variables[f"q_{alias}"] = searches[alias]
                variables[f"c_{alias}"] = cursor
            body = await self.graphql(f"query({declared}) {{ {fields} }}", variables)

            errored = _failed_aliases(body)
            next_cursors: dict[str, str | None] = {}
            for alias in cursors:
                result = body["data"].get(alias)
                if alias in errored or result is None:
                    failed.add(alias)
                    continue
                totals[alias] = result["issueCount"]
                # Non-PR results come back as empty objects.
                prs = [_remote_pr(node) for node in result["nodes"] if node]
                found[alias].update((pr.number, pr) for pr in prs)
                if on_page and prs:
                    await on_page(alias, prs)
                page = result["pageInfo"]
                if page["hasNextPage"]:
                    next_cursors[alias] = page["endCursor"]
            cursors = next_cursors

        complete: dict[str, list[RemotePR]] = {}
        for alias, prs in found.items():
            if alias in failed:
                logger.warning(f"GitHub search {searches[alias]!r} failed; skipping it this time")
            elif len(prs) != totals.get(alias):
                # Search caps at 1000 results, and PRs can shift between pages.
                logger.warning(
                    f"GitHub search {searches[alias]!r} returned {len(prs)} of "
                    f"{totals.get(alias)} results; skipping it this time"
                )
            else:
                complete[alias] = list(prs.values())
        return complete

    async def pr_statuses(self, refs: Iterable[PRRef]) -> dict[PRRef, PRStatus]:
        """Look up specific PRs (open, closed or merged). PRs that can't be read are left out."""
        refs = list(refs)
        statuses: dict[PRRef, PRStatus] = {}
        for start in range(0, len(refs), _STATUS_BATCH):
            batch = refs[start : start + _STATUS_BATCH]
            declared = ", ".join(
                f"$o{i}: String!, $n{i}: String!, $k{i}: Int!" for i in range(len(batch))
            )
            fields = " ".join(
                f"t{i}: repository(owner: $o{i}, name: $n{i}) {{"
                f" pullRequest(number: $k{i}) {{ state headRefOid }} }}"
                for i in range(len(batch))
            )
            variables: dict[str, str | int | None] = {}
            for i, ref in enumerate(batch):
                owner, _, name = ref.repo.partition("/")
                variables.update({f"o{i}": owner, f"n{i}": name, f"k{i}": ref.number})
            body = await self.graphql(f"query({declared}) {{ {fields} }}", variables)
            for i, ref in enumerate(batch):
                pr = (body["data"].get(f"t{i}") or {}).get("pullRequest")
                if pr:
                    statuses[ref] = PRStatus(
                        state=GHState(pr["state"].lower()), head_sha=pr["headRefOid"]
                    )
        return statuses
