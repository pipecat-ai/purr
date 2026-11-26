"""Read-only GitHub queries, through the GitHub CLI (``gh api graphql``).

Going through ``gh`` means Purr uses whatever GitHub login the user already has.
Everything here reads; writes (assigning, posting reviews, merging) live with
the code that executes approved actions.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeAlias
from urllib.parse import quote

from loguru import logger

from purr.compat import batched
from purr.models import (
    PR,
    ActionKind,
    Check,
    CheckState,
    FileChange,
    GHState,
    MergeCheck,
    PastPR,
    PRComment,
    PRDetails,
    PRHead,
    PRRef,
    Reply,
    Verdict,
    WaitingRun,
)

# Searches are sorted by creation so that pages stay stable while PRs get updated.
# None lists your own PRs: you work on them elsewhere (in Claude Code, say).
# A repo's list is every open PR by other people, whoever is assigned: the
# unassigned ones are the pool, the rest show who's on them.
OPEN_QUERY = "is:pr is:open archived:false draft:false -author:@me sort:created-desc repo:{repo}"
ASSIGNED_QUERY = "is:pr is:open archived:false -author:@me assignee:@me sort:created-desc"
# PRs asking for your review by name (not through a team), in any repo. Shown,
# never reviewed on their own: only being assigned starts a review.
REQUESTED_QUERY = (
    "is:pr is:open archived:false draft:false user-review-requested:@me sort:created-desc"
)
# PRs by others you've taken part in, in any repo, to keep track of replies to you.
# Commenting covers review comments; reviewing covers an approval that said nothing.
COMMENTED_QUERY = (
    "is:pr is:open archived:false draft:false -author:@me commenter:@me sort:created-desc"
)
REVIEWED_QUERY = (
    "is:pr is:open archived:false draft:false -author:@me reviewed-by:@me sort:created-desc"
)
# Every PR in a repo touched since a time, whatever its state: used between full
# syncs. Being assigned, closed or drafted updates a PR too.
UPDATED_QUERY = "is:pr archived:false -author:@me updated:>={since} sort:updated-desc repo:{repo}"
# The searches every snapshot runs besides the watched repos', by alias.
_ANY_REPO_SEARCHES = {
    "assigned": ASSIGNED_QUERY,
    "requested": REQUESTED_QUERY,
    "commented": COMMENTED_QUERY,
    "reviewed": REVIEWED_QUERY,
}

_PR_FIELDS = """
    number title url state isDraft updatedAt createdAt headRefOid baseRefName
    author { login } authorAssociation
    assignees(first: 20) { nodes { login } }
    reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } } } }
    repository { nameWithOwner }
"""
# The head's GitHub Actions workflows: those waiting for approval, and those starting
# (not in the rollup till they have a check run). Only suites with a workflow run:
# other apps' sit queued forever.
_WORKFLOWS = """
    checkSuites(first: 30) { nodes {
        status conclusion checkRuns(first: 0) { totalCount }
        workflowRun { databaseId workflow { name } }
    } }
"""
_DETAILS_FIELDS = f"""
    body baseRefName headRefName additions deletions changedFiles reviewDecision
    labels(first: 10) {{ nodes {{ name }} }}
    files(first: 100) {{ nodes {{ path additions deletions }} }}
    commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} {_WORKFLOWS} }} }} }}
    comments(last: 30) {{ nodes {{ id author {{ login }} body createdAt viewerDidAuthor }} }}
    reviews(last: 30) {{ nodes {{ id author {{ login }} body submittedAt state viewerDidAuthor }} }}
"""
# Who said what on a PR, for replies to you (see replies_to).
_CONVERSATION_FIELDS = """
    comments(last: 50) { nodes { author { login __typename } createdAt body url } }
    reviews(last: 50) { nodes { author { login } submittedAt state body } }
    reviewThreads(last: 50) { nodes { path line
        comments(last: 30) { nodes { author { login __typename } createdAt body url } } } }
"""
# A PR's inline review threads, for the diff view (see review_threads).
_THREAD_FIELDS = """
    reviewThreads(first: 100) { nodes { id isResolved isOutdated path line diffSide
        comments(first: 30) { nodes { id author { login } body createdAt } } } }
"""


def _verdict_fields(me: str) -> str:
    """Your latest verdict on a PR: the last review of yours that approved it, requested
    changes or got dismissed, whatever you said after it."""
    return (
        f" yourVerdict: reviews(last: 1, author: {json.dumps(me)},"
        " states: [APPROVED, CHANGES_REQUESTED, DISMISSED]) { nodes { state commit { oid } } }"
    )


# What you know of a repo: your merged PRs there, and those of others you reviewed,
# the latest 50 of each, with their files (see purr.upnext).
_HISTORY = {
    "wrote": "is:pr is:merged author:@me sort:updated-desc repo:{repo}",
    "reviewed": "is:pr reviewed-by:@me -author:@me sort:updated-desc repo:{repo}",
}
_HISTORY_FIELDS = "author { login } files(first: 50) { nodes { path } }"
_HISTORY_SIZE = 50
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
        involved: Every open PR by others you've commented on or reviewed, in any repo.
        updates: Incrementally synced repos: every PR touched since the last
            sync, whatever its state. Absence means "unchanged".
        viewer: Your GitHub login.

    Only complete results are included: a repo whose search failed or came back
    partial is missing from ``open``/``updates``, and ``assigned`` is None if
    that search did (``requested`` and ``involved`` likewise). Absence from an
    incomplete result says nothing about a PR.
    """

    open: dict[str, list[RemotePR]]
    assigned: list[RemotePR] | None
    updates: dict[str, list[RemotePR]] = field(default_factory=dict)
    viewer: str | None = None
    requested: list[RemotePR] | None = field(default_factory=list)
    involved: list[RemotePR] | None = field(default_factory=list)


@dataclass(frozen=True)
class Standing:
    """Where you stand on a PR on GitHub: what others said to you since your last word, and
    your latest verdict."""

    replies: list[Reply]
    verdict: Verdict | None = None


@dataclass(frozen=True)
class PRStatus:
    state: GHState
    head_sha: str


def _remote_pr(node: dict) -> RemotePR:
    return RemotePR(
        repo=node["repository"]["nameWithOwner"],
        number=node["number"],
        title=node["title"],
        author=_login(node),
        url=node["url"],
        head_sha=node["headRefOid"],
        updated_at=node["updatedAt"],
        created_at=node.get("createdAt", ""),
        is_draft=node["isDraft"],
        state=GHState(node.get("state", "OPEN").lower()),
        assignees=tuple(a["login"] for a in _nodes(node.get("assignees"))),
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


# Gets each page of search results as it arrives: ``(what was searched, prs)``.
OnPage: TypeAlias = Callable[[str, list[RemotePR]], Awaitable[None]]


@dataclass
class _Search:
    """One search's progress through its pages of results."""

    query: str
    # By repo and number: a search across repos (assigned to you, say) can find a #1 in
    # each of several.
    found: dict[PRRef, RemotePR] = field(default_factory=dict)
    total: int | None = None  # how many results GitHub says there are
    cursor: str | None = None  # where the next page starts
    more: bool = True  # whether there are pages left to fetch
    failed: bool = False

    def add_page(self, result: dict | None) -> list[RemotePR]:
        """Take in a page of results (None: the search failed); returns its PRs."""
        if result is None:
            self.failed, self.more = True, False
            return []
        self.total = result["issueCount"]
        # Non-PR results come back as empty objects.
        prs = [_remote_pr(node) for node in result["nodes"] if node]
        self.found.update((pr.ref, pr) for pr in prs)
        page = result["pageInfo"]
        self.more, self.cursor = page["hasNextPage"], page["endCursor"]
        return prs

    def results(self) -> list[RemotePR] | None:
        """Every PR found; None (and a warning) if the search failed or came back partial."""
        if self.failed:
            logger.warning(f"GitHub search {self.query!r} failed; skipping it this time")
            return None
        if len(self.found) != self.total:
            # Search caps at 1000 results, and PRs can shift between pages.
            logger.warning(
                f"GitHub search {self.query!r} returned {len(self.found)} of "
                f"{self.total} results; skipping it this time"
            )
            return None
        return list(self.found.values())


def _search_query(searches: Mapping[str, _Search]) -> tuple[str, dict[str, str | int | None]]:
    """The query for the next page of each search, under the search's alias."""
    declared = ", ".join(f"$q_{a}: String!, $c_{a}: String" for a in searches)
    fields = " ".join(
        f"{a}: search(query: $q_{a}, type: ISSUE, first: {_PAGE_SIZE}, after: $c_{a}) {{"
        f" issueCount pageInfo {{ hasNextPage endCursor }}"
        f" nodes {{ ... on PullRequest {{ {_PR_FIELDS} }} }} }}"
        for a in searches
    )
    variables: dict[str, str | int | None] = {}
    for alias, search in searches.items():
        variables[f"q_{alias}"] = search.query
        variables[f"c_{alias}"] = search.cursor
    return f"query({declared}) {{ {fields} }}", variables


def _pull_requests_query(
    refs: Sequence[PRRef], fields: str
) -> tuple[str, dict[str, str | int | None]]:
    """The query for ``fields`` of each PR, under ``t0``, ``t1``, ... in order."""
    declared = ", ".join(f"$o{i}: String!, $n{i}: String!, $k{i}: Int!" for i in range(len(refs)))
    selections = " ".join(
        f"t{i}: repository(owner: $o{i}, name: $n{i}) {{"
        f" pullRequest(number: $k{i}) {{ {fields} }} }}"
        for i in range(len(refs))
    )
    variables: dict[str, str | int | None] = {}
    for i, ref in enumerate(refs):
        owner, _, name = ref.repo.partition("/")
        variables.update({f"o{i}": owner, f"n{i}": name, f"k{i}": ref.number})
    return f"query({declared}) {{ {selections} }}", variables


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
        return await self._raw(
            f"repos/{ref.repo}/pulls/{ref.number}", "application/vnd.github.diff"
        )

    async def file_at(self, repo: str, path: str, sha: str) -> str:
        """A file as it is at commit ``sha``."""
        return await self._raw(
            f"repos/{repo}/contents/{quote(path)}?ref={sha}", "application/vnd.github.raw"
        )

    async def _raw(self, endpoint: str, accept: str) -> str:
        """What a REST endpoint answers, as text (``accept``: the kind)."""
        args = [self._gh, "api", endpoint, "-H", f"Accept: {accept}"]
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
        on_page: OnPage | None = None,
    ) -> Snapshot:
        """Fetch watched repos, and the PRs assigned to you, asking for your review or that
        you've taken part in, in as few requests as possible (one per page of results, all
        searches batched together).

        Args:
            full: Repos to fetch every listed PR of.
            incremental: Repos to fetch only PRs updated since a time (``YYYY-MM-DDTHH:MM:SSZ``).
            on_page: Called with ``(repo, prs)`` for each page of a full sync as it
                arrives, so a newly added repo can show up before all pages are in.
        """
        incremental = dict(incremental or {})
        searches = dict(_ANY_REPO_SEARCHES)
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
            involved=_involved(results),
        )

    async def _search_all(
        self, searches: dict[str, str], *, on_page: OnPage | None = None
    ) -> dict[str, list[RemotePR]]:
        """Run several searches, following pages. Returns only searches that completed."""
        progress = {alias: _Search(query) for alias, query in searches.items()}
        while pending := {alias: search for alias, search in progress.items() if search.more}:
            await self._next_page(pending, on_page)
        results = {alias: search.results() for alias, search in progress.items()}
        return {alias: prs for alias, prs in results.items() if prs is not None}

    async def _next_page(self, searches: dict[str, _Search], on_page: OnPage | None) -> None:
        """Fetch the next page of each search, all in one request."""
        body = await self.graphql(*_search_query(searches))
        errored = _failed_aliases(body)
        for alias, search in searches.items():
            prs = search.add_page(None if alias in errored else body["data"].get(alias))
            if on_page and prs:
                await on_page(alias, prs)

    async def history(self, repo: str) -> list[PastPR]:
        """Your latest merged PRs in ``repo``, and those of others you reviewed (50 of each),
        with the files they changed: one request."""
        declared = ", ".join(f"$q_{alias}: String!" for alias in _HISTORY)
        fields = " ".join(
            f"{alias}: search(query: $q_{alias}, type: ISSUE, first: {_HISTORY_SIZE}) {{"
            f" nodes {{ ... on PullRequest {{ {_HISTORY_FIELDS} }} }} }}"
            for alias in _HISTORY
        )
        variables: dict[str, str | int | None] = {
            f"q_{alias}": query.format(repo=repo) for alias, query in _HISTORY.items()
        }
        body = await self.graphql(f"query({declared}) {{ {fields} }}", variables)
        return [
            PastPR(
                _login(node), tuple(f["path"] for f in _nodes(node.get("files"))), alias == "wrote"
            )
            for alias in _HISTORY
            for node in _nodes(body["data"].get(alias))
        ]

    async def review_threads(self, ref: PRRef) -> list[dict]:
        """A PR's inline review threads, with their comments; ``by_user`` marks yours."""
        pr = (await self._pull_requests([ref], _THREAD_FIELDS)).get(ref) or {}
        viewer = (await self.viewer_login()).lower()
        return [_thread(node, viewer) for node in _nodes(pr.get("reviewThreads"))]

    async def pr_statuses(self, refs: Iterable[PRRef]) -> dict[PRRef, PRStatus]:
        """Look up specific PRs (open, closed or merged). PRs that can't be read are left out."""
        found = await self._pull_requests(refs, "state headRefOid", batch_size=_STATUS_BATCH)
        return {
            ref: PRStatus(state=GHState(pr["state"].lower()), head_sha=pr["headRefOid"])
            for ref, pr in found.items()
        }

    async def merge_check(self, ref: PRRef) -> MergeCheck | None:
        """Where the PR stands for merging, now; None if GitHub doesn't show it."""
        found = await self._pull_requests([ref], _merge_fields(ref.number))
        return _merge_check(found[ref]) if ref in found else None

    async def pr_head(self, ref: PRRef) -> PRHead | None:
        """Where the PR's commits live, now; None if GitHub doesn't show it."""
        fields = "headRefName headRefOid maintainerCanModify headRepository { nameWithOwner }"
        found = (await self._pull_requests([ref], fields)).get(ref)
        if found is None:
            return None
        return PRHead(
            repo=(found.get("headRepository") or {}).get("nameWithOwner"),
            branch=found["headRefName"],
            sha=found["headRefOid"],
            maintainer_can_modify=bool(found.get("maintainerCanModify")),
        )

    async def standing(self, refs: Iterable[PRRef]) -> dict[PRRef, Standing]:
        """What others said to you on these PRs since your last word (see :func:`replies_to`),
        and your latest verdict on each (see :func:`verdict_of`).

        PRs that can't be read are left out.
        """
        me = await self.viewer_login()
        fields = _CONVERSATION_FIELDS + _verdict_fields(me)
        return {
            ref: Standing(replies_to(me, pr), verdict_of(pr))
            for ref, pr in (await self._pull_requests(refs, fields)).items()
        }

    async def _pull_requests(
        self, refs: Iterable[PRRef], fields: str, *, batch_size: int = _DETAILS_BATCH
    ) -> dict[PRRef, dict]:
        """``fields`` of specific PRs, a batch per request. PRs that can't be read are left out."""
        found: dict[PRRef, dict] = {}
        for batch in batched(refs, batch_size):
            body = await self.graphql(*_pull_requests_query(batch, fields))
            answers = [
                (ref, (body["data"].get(f"t{i}") or {}).get("pullRequest"))
                for i, ref in enumerate(batch)
            ]
            found.update((ref, pr) for ref, pr in answers if pr)
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
    comments = _nodes(pr.get("comments"))
    threads = [
        (thread, _nodes(thread.get("comments"))) for thread in _nodes(pr.get("reviewThreads"))
    ]
    reviews = [r for r in _nodes(pr.get("reviews")) if r.get("submittedAt")]
    yours = [r for r in reviews if _by(me, r)]
    on_pr = [c["createdAt"] for c in comments if _by(me, c)]
    on_pr += [r["submittedAt"] for r in yours if _says_something(r)]
    in_threads = [c["createdAt"] for _, nodes in threads for c in nodes if _by(me, c)]
    if not on_pr and not in_threads and not yours:
        return []
    last_word = max([*on_pr, *in_threads, *(r["submittedAt"] for r in yours)])
    replies = [_reply(c) for c in comments if _replies_to_me(me, c, last_word)]
    for thread, nodes in threads:
        replies += _thread_replies(me, thread, nodes, on_pr)
    return sorted(replies, key=lambda r: r.at)


_VERDICT_KINDS = {"APPROVED": ActionKind.APPROVE, "CHANGES_REQUESTED": ActionKind.REQUEST_CHANGES}


def verdict_of(pr: dict) -> Verdict | None:
    """Your latest verdict on a PR (its GraphQL node, with ``yourVerdict``), at the commit you
    gave it: None if you've given none, or it was dismissed."""
    latest = _nodes(pr.get("yourVerdict"))[-1:]
    if not latest:
        return None
    kind = _VERDICT_KINDS.get(latest[0].get("state") or "")
    head = (latest[0].get("commit") or {}).get("oid")
    return Verdict(kind, head) if kind and head else None


def _says_something(review: dict) -> bool:
    """A review with text, or that decides (one that's only thread comments doesn't)."""
    return bool((review.get("body") or "").strip()) or review.get("state") != "COMMENTED"


def _thread_replies(me: str, thread: dict, comments: list[dict], on_pr: list[str]) -> list[Reply]:
    """Replies to ``me`` in a review thread they're in, after their last comment in it or
    their last word on the PR (``on_pr``: when they said something there), whichever is
    later."""
    mine = [c["createdAt"] for c in comments if _by(me, c)]
    if not mine:
        return []
    after = max([*mine, *on_pr])
    path, line = thread.get("path") or "", thread.get("line")
    where = f"{path}:{line}" if path and line else path
    return [_reply(c, where) for c in comments if _replies_to_me(me, c, after)]


def _by(me: str, node: dict) -> bool:
    """Whether ``me`` (lowercase) wrote this comment or review."""
    return (node.get("author") or {}).get("login", "").lower() == me


def _replies_to_me(me: str, comment: dict, after: str) -> bool:
    """Whether a comment counts as a reply: someone else's, not a bot's, and after ``after``."""
    author = comment.get("author") or {}
    return (
        not _by(me, comment) and author.get("__typename") != "Bot" and comment["createdAt"] > after
    )


def _reply(comment: dict, where: str = "") -> Reply:
    body = _clip(comment.get("body") or "", 500)
    return Reply(_login(comment), body, comment["createdAt"], comment.get("url") or "", where)


def _thread(node: dict, viewer: str) -> dict:
    """A review thread and its comments; ``viewer`` is your login, lowercase."""
    return {
        "id": node["id"],
        "path": node.get("path"),
        "line": node.get("line"),
        "side": node.get("diffSide") or "RIGHT",  # LEFT: on a deleted line
        "resolved": node.get("isResolved", False),
        "outdated": node.get("isOutdated", False),
        "comments": [_thread_comment(c, viewer) for c in _nodes(node.get("comments"))],
    }


def _thread_comment(comment: dict, viewer: str) -> dict:
    login = _login(comment)
    return {
        "id": comment.get("id", ""),
        "author": login,
        "by_user": login.lower() == viewer,
        "body": _clip(comment.get("body", "")),
        "at": comment.get("createdAt"),
    }


def _involved(results: Mapping[str, list[RemotePR]]) -> list[RemotePR] | None:
    """PRs you commented on or reviewed, once each; None unless both searches completed."""
    if "commented" not in results or "reviewed" not in results:
        return None
    both = {pr.ref: pr for pr in [*results["commented"], *results["reviewed"]]}
    return list(both.values())


def _merge_fields(number: int) -> str:
    """What merging looks at. Whether a check is required depends on the PR's base."""
    required = f"isRequired(pullRequestNumber: {number})"
    return f"""
        headRefOid baseRefName mergeable mergeStateStatus
        baseRepository {{ viewerDefaultMergeMethod }}
        assignees(first: 20) {{ nodes {{ login }} }}
        latestOpinionatedReviews(first: 20) {{ nodes {{ author {{ login }} state }} }}
        commits(last: 1) {{ nodes {{ commit {{ {_WORKFLOWS}
            statusCheckRollup {{ contexts(first: 100) {{
            nodes {{
                ... on CheckRun {{ name status conclusion startedAt completedAt {required} }}
                ... on StatusContext {{ context state createdAt {required} }}
            }}
        }} }} }} }} }}
    """


# A finished check run's conclusion, or a status's state, as Purr sees it.
_CHECK_STATES = {
    "SUCCESS": CheckState.PASSED,
    "NEUTRAL": CheckState.PASSED,
    "SKIPPED": CheckState.SKIPPED,
    "PENDING": CheckState.RUNNING,
    "EXPECTED": CheckState.RUNNING,
}


def _check(node: dict) -> Check | None:
    """A check run or a status; None for anything else in the rollup."""
    if "context" in node:  # a status, as readthedocs reports: when it said so last
        name, outcome = node["context"], node.get("state") or ""
        pending = outcome in ("PENDING", "EXPECTED")
        started, finished = (node.get("createdAt") or "", "") if pending else ("", "")
    elif "name" in node:
        name = node["name"]
        done = node.get("status") == "COMPLETED"
        outcome = (node.get("conclusion") or "") if done else "PENDING"
        started, finished = node.get("startedAt") or "", node.get("completedAt") or ""
    else:
        return None
    state = _CHECK_STATES.get(outcome, CheckState.FAILED)  # failure, error, timed out, ...
    required = bool(node.get("isRequired"))
    return Check(name, state, required=required, started=started, finished=finished)


# A workflow's suite that's starting: not WAITING, which is on a deployment's approval.
_STARTING = frozenset({"REQUESTED", "QUEUED", "PENDING", "IN_PROGRESS"})


def _workflows(commit: dict) -> tuple[list[WaitingRun], list[Check]]:
    """The head's workflow runs waiting for approval, and those starting, as checks still
    running: the rollup has no word of them till their first check run."""
    waiting, starting = [], []
    for suite in _nodes(commit.get("checkSuites")):
        if not (run := suite.get("workflowRun")):
            continue
        name = (run.get("workflow") or {}).get("name") or "workflow"
        runs = (suite.get("checkRuns") or {}).get("totalCount")
        if suite.get("conclusion") == "ACTION_REQUIRED":
            waiting.append(WaitingRun(run["databaseId"], name))
        elif suite.get("status") in _STARTING and not runs:
            starting.append(Check(name, CheckState.RUNNING))
    return waiting, starting


def _head(pr: dict) -> dict:
    commits = _nodes(pr.get("commits"))
    return (commits[0].get("commit") or {}) if commits else {}


def _merge_check(pr: dict) -> MergeCheck:
    head = _head(pr)
    rollup = head.get("statusCheckRollup")
    checks = [_check(node) for node in _nodes((rollup or {}).get("contexts"))]
    waiting, starting = _workflows(head)
    reviews = [
        (_login(review), review.get("state") or "")
        for review in _nodes(pr.get("latestOpinionatedReviews"))
    ]
    return MergeCheck(
        head_sha=pr.get("headRefOid") or "",
        base=pr.get("baseRefName") or "",
        method=(
            (pr.get("baseRepository") or {}).get("viewerDefaultMergeMethod") or "MERGE"
        ).lower(),
        mergeable=pr.get("mergeable"),
        merge_state=pr.get("mergeStateStatus"),
        checks=(*(check for check in checks if check), *starting),
        assignees=tuple(a["login"] for a in _nodes(pr.get("assignees"))),
        reviews=tuple(reviews),
        waiting=tuple(waiting),
    )


def _ci(head: dict) -> str | None:
    """The head's CI, as the header says it: the rollup's state, unless workflows wait for
    approval (then CI hasn't run, whatever the rest says) or are starting (only a failure
    says more)."""
    state = (head.get("statusCheckRollup") or {}).get("state")
    waiting, starting = _workflows(head)
    if waiting:
        return "ACTION_REQUIRED"
    return "PENDING" if starting and state in (None, "SUCCESS") else state


def _details(pr: dict) -> PRDetails:
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
        checks=_ci(_head(pr)),
        conversation=_conversation(pr),
    )


def _conversation(pr: dict) -> tuple[PRComment, ...]:
    """The PR's comments, and its reviews' text (those that say something), oldest first."""
    said = [
        PRComment(c["id"], "comment", _login(c), _clip(c.get("body") or ""), c["createdAt"],
                  mine=bool(c.get("viewerDidAuthor")))
        for c in _nodes(pr.get("comments"))
    ]  # fmt: skip
    said += [
        PRComment(r["id"], "review", _login(r), _clip(r["body"]), r["submittedAt"],
                  mine=bool(r.get("viewerDidAuthor")), state=r.get("state") or "")
        for r in _nodes(pr.get("reviews"))
        if (r.get("body") or "").strip() and r.get("submittedAt")
    ]  # fmt: skip
    return tuple(sorted(said, key=lambda comment: comment.at))


def _login(node: dict) -> str:
    return (node.get("author") or {}).get("login") or "ghost"


def _clip(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
