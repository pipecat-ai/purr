"""Read-only GitHub queries, through the GitHub CLI (``gh api graphql``).

Going through ``gh`` means Purr uses whatever GitHub login the user already has.
Everything here reads; writes (assigning, posting reviews, merging) live with
the code that executes approved actions.
"""

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger

from purr.models import PR, GHState, PRRef

# Searches are sorted by creation so that pages stay stable while PRs get updated.
POOL_QUERY = (
    "is:pr is:open archived:false draft:false no:assignee -author:@me sort:created-desc repo:{repo}"
)
ASSIGNED_QUERY = "is:pr is:open archived:false assignee:@me sort:created-desc"

_PR_FIELDS = """
    number title url isDraft updatedAt headRefOid
    author { login }
    repository { nameWithOwner }
"""
_PAGE_SIZE = 100
_STATUS_BATCH = 50


class GitHubError(Exception):
    """GitHub couldn't be queried (gh missing, not logged in, network, ...)."""


@dataclass(frozen=True)
class RemotePR:
    """An open PR as GitHub reports it."""

    repo: str
    number: int
    title: str
    author: str
    url: str
    head_sha: str
    updated_at: str
    is_draft: bool = False

    @property
    def ref(self) -> PRRef:
        return PRRef(self.repo, self.number)

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

    Only complete results are included: a repo whose search failed or came back
    partial is missing from ``pool``, and ``assigned`` is None if that search
    did. Absence from an incomplete result says nothing about a PR.
    """

    pool: dict[str, list[RemotePR]]
    assigned: list[RemotePR] | None


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

    async def snapshot(self, repos: Iterable[str]) -> Snapshot:
        """Fetch every watched repo's pool and the PRs assigned to you, in as few requests
        as possible (one per page of results)."""
        searches = {"assigned": ASSIGNED_QUERY}
        aliases = {}
        for i, repo in enumerate(repos):
            aliases[f"r{i}"] = repo
            searches[f"r{i}"] = POOL_QUERY.format(repo=repo)

        results = await self._search_all(searches)
        pool = {aliases[alias]: prs for alias, prs in results.items() if alias in aliases}
        return Snapshot(pool=pool, assigned=results.get("assigned"))

    async def _search_all(self, searches: dict[str, str]) -> dict[str, list[RemotePR]]:
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
                for node in result["nodes"]:
                    if node:  # non-PR results come back as empty objects
                        pr = _remote_pr(node)
                        found[alias][pr.number] = pr
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
