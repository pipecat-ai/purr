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

from purr.models import PR, FileChange, GHState, PRDetails, PRRef, Reply

# Searches are sorted by creation so that pages stay stable while PRs get updated.
# A repo's list is every open PR by other people, whoever is assigned: the
# unassigned ones are the pool, the rest show who's on them.
OPEN_QUERY = "is:pr is:open archived:false draft:false -author:@me sort:created-desc repo:{repo}"
ASSIGNED_QUERY = "is:pr is:open archived:false assignee:@me sort:created-desc"
# PRs asking for your review by name (not through a team), in any repo. Shown,
# never reviewed on their own: only being assigned starts a review.
REQUESTED_QUERY = (
    "is:pr is:open archived:false draft:false user-review-requested:@me sort:created-desc"
)
# Every PR in a repo touched since a time, whatever its state: used between full
# syncs. Being assigned, closed or drafted updates a PR too.
UPDATED_QUERY = "is:pr archived:false updated:>={since} sort:updated-desc repo:{repo}"

_PR_FIELDS = """
    number title url state isDraft updatedAt createdAt headRefOid baseRefName
    author { login } authorAssociation
    assignees(first: 20) { nodes { login } }
    reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } } } }
    repository { nameWithOwner }
"""
_DETAILS_FIELDS = """
    body baseRefName headRefName additions deletions changedFiles reviewDecision
    labels(first: 10) { nodes { name } }
    files(first: 100) { nodes { path additions deletions } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
"""
# Who said what on a PR, for replies to you (see replies_to).
_CONVERSATION_FIELDS = """
    comments(last: 50) { nodes { author { login __typename } createdAt body url } }
    reviews(last: 50) { nodes { author { login __typename } submittedAt state body url } }
    reviewThreads(last: 50) { nodes { path line
        comments(last: 30) { nodes { author { login __typename } createdAt body url } } } }
"""
_PAGE_SIZE = 100
_STATUS_BATCH = 50
_DETAILS_BATCH = 20


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
    created_at: str = ""
    is_draft: bool = False
    state: GHState = GHState.OPEN
    assignees: tuple[str, ...] = ()
    requested: tuple[str, ...] = ()  # people asked to review it (teams aren't listed)
    base: str = ""  # the branch it merges into
    author_association: str = ""  # OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, NONE, ...

    @property
    def ref(self) -> PRRef:
        return PRRef(self.repo, self.number)

    def listed(self, me: str) -> bool:
        """Shown in its repo: open, ready for review, and not yours."""
        return (
            self.state == GHState.OPEN and not self.is_draft and self.author.lower() != me.lower()
        )

    def in_pool(self, me: str) -> bool:
        """Listed, and nobody is assigned."""
        return self.listed(me) and not self.assignees

    def to_pr(self, me: str) -> PR:
        """As a stored PR; ``me`` is your login, to tell whether it's assigned to you."""
        return PR(
            repo=self.repo,
            number=self.number,
            title=self.title,
            author=self.author,
            url=self.url,
            head_sha=self.head_sha,
            updated_at=self.updated_at,
            created_at=self.created_at,
            base=self.base,
            author_association=self.author_association,
            assignees=self.assignees,
            assigned_to_me=me.lower() in {a.lower() for a in self.assignees},
            requested_from_me=me.lower() in {r.lower() for r in self.requested},
        )


@dataclass
class Snapshot:
    """One poll's view of GitHub.

    Parameters:
        open: Fully synced repos: every listed PR in each (open, not a draft,
            not yours), whoever is assigned.
        assigned: Every open PR assigned to you, in any repo.
        requested: Every open PR asking for your review by name, in any repo.
        updates: Incrementally synced repos: every PR touched since the last
            sync, whatever its state. Absence means "unchanged".
        viewer: Your GitHub login.

    Only complete results are included: a repo whose search failed or came back
    partial is missing from ``open``/``updates``, and ``assigned`` is None if
    that search did (``requested`` likewise). Absence from an incomplete result
    says nothing about a PR.
    """

    open: dict[str, list[RemotePR]]
    assigned: list[RemotePR] | None
    updates: dict[str, list[RemotePR]] = field(default_factory=dict)
    viewer: str | None = None
    requested: list[RemotePR] | None = field(default_factory=list)


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
        created_at=node.get("createdAt", ""),
        is_draft=node["isDraft"],
        state=GHState(node.get("state", "OPEN").lower()),
        assignees=tuple(a["login"] for a in (node.get("assignees") or {}).get("nodes", [])),
        requested=tuple(
            login
            for request in _nodes(node.get("reviewRequests"))
            if (login := (request.get("requestedReviewer") or {}).get("login"))
        ),
        base=node.get("baseRefName") or "",
        author_association=node.get("authorAssociation") or "",
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

    async def pr_diff(self, ref: PRRef) -> str:
        """The PR's unified diff (what ``gh pr diff`` shows). GitHub refuses very big ones."""
        args = [self._gh, "api", f"repos/{ref.repo}/pulls/{ref.number}"]
        args += ["-H", "Accept: application/vnd.github.diff"]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError as e:
            raise GitHubError(f"GitHub CLI not found ({self._gh})") from e
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip().removeprefix("gh: ")
            raise GitHubError(message or f"gh exited with {process.returncode}")
        return stdout.decode(errors="replace")

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
        """Fetch watched repos and the PRs assigned to you or asking for your review, in as
        few requests as possible (one per page of results, all searches batched together).

        Args:
            full: Repos to fetch every listed PR of.
            incremental: Repos to fetch only PRs updated since a time (``YYYY-MM-DDTHH:MM:SSZ``).
            on_page: Called with ``(repo, prs)`` for each page of a full sync as it
                arrives, so a newly added repo can show up before all pages are in.
        """
        incremental = dict(incremental or {})
        searches = {"assigned": ASSIGNED_QUERY, "requested": REQUESTED_QUERY}
        full_aliases: dict[str, str] = {}
        updated_aliases: dict[str, str] = {}
        for i, repo in enumerate(full):
            full_aliases[f"r{i}"] = repo
            searches[f"r{i}"] = OPEN_QUERY.format(repo=repo)
        for i, (repo, since) in enumerate(incremental.items()):
            updated_aliases[f"u{i}"] = repo
            searches[f"u{i}"] = UPDATED_QUERY.format(repo=repo, since=since)

        async def page(alias: str, prs: list[RemotePR]) -> None:
            if on_page and alias in full_aliases:
                await on_page(full_aliases[alias], prs)

        viewer = await self.viewer_login()
        results = await self._search_all(searches, on_page=page)
        return Snapshot(
            open={full_aliases[a]: prs for a, prs in results.items() if a in full_aliases},
            assigned=results.get("assigned"),
            updates={updated_aliases[a]: prs for a, prs in results.items() if a in updated_aliases},
            viewer=viewer,
            requested=results.get("requested"),
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

    async def review_threads(self, ref: PRRef) -> list[dict]:
        """A PR's inline review threads, with their comments; ``by_user`` marks yours."""
        owner, _, name = ref.repo.partition("/")
        body = await self.graphql(
            "query($owner: String!, $name: String!, $number: Int!) {"
            " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
            " reviewThreads(first: 100) { nodes { id isResolved isOutdated path line"
            " comments(first: 30) { nodes { author { login } body createdAt } } } } } } }",
            {"owner": owner, "name": name, "number": ref.number},
        )
        viewer = (await self.viewer_login()).lower()
        pr = (body["data"].get("repository") or {}).get("pullRequest") or {}
        threads = []
        for node in (pr.get("reviewThreads") or {}).get("nodes") or []:
            comments = []
            for comment in (node.get("comments") or {}).get("nodes") or []:
                login = (comment.get("author") or {}).get("login", "ghost")
                comments.append(
                    {
                        "author": login,
                        "by_user": login.lower() == viewer,
                        "body": _clip(comment.get("body", "")),
                        "at": comment.get("createdAt"),
                    }
                )
            threads.append(
                {
                    "id": node["id"],
                    "path": node.get("path"),
                    "line": node.get("line"),
                    "resolved": node.get("isResolved", False),
                    "outdated": node.get("isOutdated", False),
                    "comments": comments,
                }
            )
        return threads

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

    async def replies(self, refs: Iterable[PRRef]) -> dict[PRRef, list[Reply]]:
        """What others said to you on these PRs since your last word (see :func:`replies_to`).

        PRs that can't be read are left out.
        """
        me = await self.viewer_login()
        return {
            ref: replies_to(me, pr)
            for ref, pr in (await self._pull_requests(refs, _CONVERSATION_FIELDS)).items()
        }

    async def _pull_requests(self, refs: Iterable[PRRef], fields: str) -> dict[PRRef, dict]:
        """``fields`` of specific PRs, a batch per request. PRs that can't be read are left out."""
        refs = list(refs)
        found: dict[PRRef, dict] = {}
        for start in range(0, len(refs), _DETAILS_BATCH):
            batch = refs[start : start + _DETAILS_BATCH]
            declared = ", ".join(
                f"$o{i}: String!, $n{i}: String!, $k{i}: Int!" for i in range(len(batch))
            )
            selections = " ".join(
                f"t{i}: repository(owner: $o{i}, name: $n{i}) {{"
                f" pullRequest(number: $k{i}) {{ {fields} }} }}"
                for i in range(len(batch))
            )
            variables: dict[str, str | int | None] = {}
            for i, ref in enumerate(batch):
                owner, _, name = ref.repo.partition("/")
                variables.update({f"o{i}": owner, f"n{i}": name, f"k{i}": ref.number})
            body = await self.graphql(f"query({declared}) {{ {selections} }}", variables)
            for i, ref in enumerate(batch):
                if pr := (body["data"].get(f"t{i}") or {}).get("pullRequest"):
                    found[ref] = pr
        return found

    async def pr_details(self, refs: Iterable[PRRef]) -> dict[PRRef, PRDetails]:
        """Description, files and CI state of specific PRs. PRs that can't be read are left out."""
        found = await self._pull_requests(refs, _DETAILS_FIELDS)
        return {ref: _details(pr) for ref, pr in found.items()}


def _nodes(connection: dict | None) -> list[dict]:
    return [node for node in (connection or {}).get("nodes") or [] if node]


def replies_to(me: str, pr: dict) -> list[Reply]:
    """What others said to ``me`` on a PR (its GraphQL node) after their last word, oldest first.

    - On the PR itself: anyone's comments after anything you said there.
    - In a review thread you're in: anyone's comments after your last one in it, or
      your last word on the PR (a comment, or a review that says something: one
      that's only thread comments doesn't answer the other threads).

    Bots don't count, and nothing does until you've said something.
    """
    me = me.lower()

    def by_me(node: dict) -> bool:
        return (node.get("author") or {}).get("login", "").lower() == me

    def counts(node: dict, after: str) -> bool:
        author = node.get("author") or {}
        return not by_me(node) and author.get("__typename") != "Bot" and node["createdAt"] > after

    def reply(node: dict, where: str = "") -> Reply:
        login = (node.get("author") or {}).get("login", "ghost")
        return Reply(
            login,
            _clip(node.get("body") or "", 500),
            node["createdAt"],
            node.get("url") or "",
            where,
        )

    comments = _nodes(pr.get("comments"))
    threads = [
        (thread, _nodes(thread.get("comments"))) for thread in _nodes(pr.get("reviewThreads"))
    ]
    reviews = [r for r in _nodes(pr.get("reviews")) if by_me(r) and r.get("submittedAt")]
    on_pr = [c["createdAt"] for c in comments if by_me(c)]
    on_pr += [
        r["submittedAt"]
        for r in reviews
        if (r.get("body") or "").strip() or r.get("state") != "COMMENTED"
    ]
    in_threads = [c["createdAt"] for _, nodes in threads for c in nodes if by_me(c)]
    if not on_pr and not in_threads and not reviews:
        return []
    last_word = max([*on_pr, *in_threads, *(r["submittedAt"] for r in reviews)])
    replies = [reply(c) for c in comments if counts(c, last_word)]
    for thread, nodes in threads:
        mine = [c["createdAt"] for c in nodes if by_me(c)]
        if not mine:
            continue
        after = max([*mine, *on_pr])
        path, line = thread.get("path") or "", thread.get("line")
        where = f"{path}:{line}" if path and line else path
        replies += [reply(c, where) for c in nodes if counts(c, after)]
    return sorted(replies, key=lambda r: r.at)


def _details(pr: dict) -> PRDetails:
    commits = _nodes(pr.get("commits"))
    rollup = (commits[0].get("commit") or {}).get("statusCheckRollup") if commits else None
    return PRDetails(
        body=pr.get("body") or "",
        base=pr.get("baseRefName") or "",
        head=pr.get("headRefName") or "",
        additions=pr.get("additions") or 0,
        deletions=pr.get("deletions") or 0,
        changed_files=pr.get("changedFiles") or 0,
        files=tuple(
            FileChange(f["path"], f.get("additions") or 0, f.get("deletions") or 0)
            for f in _nodes(pr.get("files"))
        ),
        labels=tuple(label["name"] for label in _nodes(pr.get("labels"))),
        review_decision=pr.get("reviewDecision"),
        checks=(rollup or {}).get("state"),
    )


def _clip(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
