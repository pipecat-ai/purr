"""The GitHub client's paging and error handling, against canned GraphQL responses."""

import pytest

from purr.github import ASSIGNED_QUERY, OPEN_QUERY, UPDATED_QUERY, GitHub, GitHubError, PRStatus
from purr.models import FileChange, GHState, PRDetails, PRRef


def node(number: int, repo: str = "pipecat-ai/pipecat", **overrides) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "state": "OPEN",
        "isDraft": False,
        "updatedAt": "2026-11-20T10:00:00Z",
        "headRefOid": "abc",
        "author": {"login": "someone"},
        "assignees": {"nodes": []},
        "repository": {"nameWithOwner": repo},
    } | overrides


def page(nodes: list[dict], total: int, cursor: str | None = None) -> dict:
    return {
        "issueCount": total,
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        "nodes": nodes,
    }


class FakeGitHub(GitHub):
    """Answers searches from a table of {query: [pages]}; records the calls."""

    def __init__(self, searches: dict[str, list[dict] | str]):
        super().__init__(gh="gh-not-used")
        self.searches = searches
        self.calls: list[dict] = []

    async def graphql(self, query, variables):
        if "viewer" in query:
            return {"data": {"viewer": {"login": "me"}}}
        self.calls.append(variables)
        data, errors = {}, []
        for key, value in variables.items():
            if not key.startswith("q_"):
                continue
            alias = key[2:]
            pages = self.searches[value]
            if isinstance(pages, str):  # an error message
                data[alias] = None
                errors.append({"path": [alias], "message": pages})
                continue
            cursor = variables.get(f"c_{alias}")
            data[alias] = pages[int(cursor) if cursor else 0]
        return {"data": data, "errors": errors} if errors else {"data": data}


def open_query(repo: str) -> str:
    return OPEN_QUERY.format(repo=repo)


async def test_snapshot_follows_pages():
    github = FakeGitHub(
        {
            ASSIGNED_QUERY: [page([node(9, "other/repo")], total=1)],
            open_query("pipecat-ai/pipecat"): [
                page([node(1), node(2)], total=3, cursor="1"),
                page([node(3)], total=3),
            ],
            open_query("pipecat-ai/whisker"): [page([], total=0)],
        }
    )
    snapshot = await github.snapshot(["pipecat-ai/pipecat", "pipecat-ai/whisker"])

    assert [pr.number for pr in snapshot.open["pipecat-ai/pipecat"]] == [1, 2, 3]
    assert snapshot.open["pipecat-ai/whisker"] == []
    assert [pr.ref for pr in snapshot.assigned] == [PRRef("other/repo", 9)]
    # One request for the first pages, a second only for the repo with more.
    assert len(github.calls) == 2
    assert [k for k in github.calls[1] if k.startswith("q_")] == ["q_r0"]


async def test_snapshot_drops_failed_and_incomplete_searches():
    github = FakeGitHub(
        {
            ASSIGNED_QUERY: "rate limited",
            open_query("pipecat-ai/pipecat"): [page([node(1)], total=2)],  # one missing
        }
    )
    snapshot = await github.snapshot(["pipecat-ai/pipecat"])
    assert snapshot.assigned is None
    assert snapshot.open == {}


async def test_pr_statuses_and_repo_name():
    class Statuses(GitHub):
        async def graphql(self, query, variables):
            if "nameWithOwner" in query:
                found = variables["name"] == "pipecat"
                return {
                    "data": {
                        "repository": {"nameWithOwner": "pipecat-ai/pipecat"} if found else None
                    }
                }
            return {
                "data": {
                    "t0": {"pullRequest": {"state": "MERGED", "headRefOid": "abc"}},
                    "t1": None,
                }
            }

    github = Statuses()
    statuses = await github.pr_statuses([PRRef("a/b", 1), PRRef("a/b", 2)])
    assert statuses == {PRRef("a/b", 1): PRStatus(GHState.MERGED, "abc")}
    assert await github.repo_name("Pipecat-AI/pipecat") == "pipecat-ai/pipecat"
    assert await github.repo_name("pipecat-ai/nope") is None


async def test_missing_gh_is_a_github_error():
    with pytest.raises(GitHubError, match="GitHub CLI not found"):
        await GitHub(gh="definitely-not-gh-purr").graphql("query { viewer { login } }", {})


async def test_snapshot_incremental_and_pages():
    since = "2026-11-20T10:00:00Z"
    github = FakeGitHub(
        {
            ASSIGNED_QUERY: [page([], total=0)],
            open_query("a/full"): [
                page([node(1, "a/full")], total=2, cursor="1"),
                page([node(2, "a/full")], total=2),
            ],
            UPDATED_QUERY.format(repo="a/inc", since=since): [
                page(
                    [
                        node(5, "a/inc"),
                        node(6, "a/inc", state="MERGED"),
                        node(7, "a/inc", assignees={"nodes": [{"login": "teammate"}]}),
                        node(8, "a/inc", isDraft=True),
                        node(9, "a/inc", author={"login": "ME"}),
                    ],
                    total=5,
                )
            ],
        }
    )
    pages = []

    async def on_page(repo, prs):
        pages.append((repo, [pr.number for pr in prs]))

    snapshot = await github.snapshot(["a/full"], {"a/inc": since}, on_page=on_page)

    assert pages == [("a/full", [1]), ("a/full", [2])]
    assert snapshot.viewer == "me"
    assert list(snapshot.open) == ["a/full"]
    updates = snapshot.updates["a/inc"]
    assert [pr.number for pr in updates if pr.listed(snapshot.viewer)] == [5, 7]
    assert [pr.number for pr in updates if pr.in_pool(snapshot.viewer)] == [5]


async def test_review_threads():
    class Threads(GitHub):
        async def graphql(self, query, variables):
            if "viewer" in query:
                return {"data": {"viewer": {"login": "Me"}}}
            comment = {"author": {"login": "me"}, "body": "Race?", "createdAt": "t"}
            thread = {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "path": "bus.py",
                "line": 42,
                "comments": {"nodes": [comment]},
            }
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [thread]}}}}}

    [thread] = await Threads().review_threads(PRRef("a/b", 1))
    assert thread["id"] == "T1" and thread["path"] == "bus.py" and not thread["resolved"]
    assert thread["comments"] == [{"author": "me", "by_user": True, "body": "Race?", "at": "t"}]


async def test_pr_details():
    class Details(GitHub):
        async def graphql(self, query, variables):
            assert variables == {"o0": "a", "n0": "b", "k0": 1, "o1": "a", "n1": "b", "k1": 2}
            pr = {
                "body": "Fixes the bus.",
                "baseRefName": "main",
                "headRefName": "fix-bus",
                "additions": 22,
                "deletions": 4,
                "changedFiles": 1,
                "reviewDecision": None,
                "labels": {"nodes": [{"name": "bug"}]},
                "files": {"nodes": [{"path": "bus.py", "additions": 22, "deletions": 4}]},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
            }
            return {"data": {"t0": {"pullRequest": pr}, "t1": {"pullRequest": None}}}

    details = await Details().pr_details([PRRef("a/b", 1), PRRef("a/b", 2)])
    assert details == {
        PRRef("a/b", 1): PRDetails(
            body="Fixes the bus.",
            base="main",
            head="fix-bus",
            additions=22,
            deletions=4,
            changed_files=1,
            files=(FileChange("bus.py", 22, 4),),
            labels=("bug",),
            checks="FAILURE",
        )
    }


async def test_pr_diff(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text(
        '#!/bin/sh\nif [ "$2" = "repos/a/b/pulls/2" ]; then echo "diff --git a/x b/x"; exit 0; fi\n'
        'echo "gh: Sorry, the diff exceeded the maximum number of files (300)" >&2; exit 1\n'
    )
    gh.chmod(0o755)
    github = GitHub(gh=str(gh))
    assert await github.pr_diff(PRRef("a/b", 2)) == "diff --git a/x b/x\n"
    with pytest.raises(GitHubError, match="^Sorry, the diff exceeded"):
        await github.pr_diff(PRRef("a/b", 3))
