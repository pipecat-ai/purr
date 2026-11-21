"""The GitHub client's paging and error handling, against canned GraphQL responses."""

import pytest

from purr.github import ASSIGNED_QUERY, GitHub, GitHubError, PRStatus
from purr.models import GHState, PRRef


def node(number: int, repo: str = "pipecat-ai/pipecat") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "updatedAt": "2026-11-20T10:00:00Z",
        "headRefOid": "abc",
        "author": {"login": "someone"},
        "repository": {"nameWithOwner": repo},
    }


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


def pool_query(repo: str) -> str:
    return (
        "is:pr is:open archived:false draft:false no:assignee -author:@me sort:created-desc "
        f"repo:{repo}"
    )


async def test_snapshot_follows_pages():
    github = FakeGitHub(
        {
            ASSIGNED_QUERY: [page([node(9, "other/repo")], total=1)],
            pool_query("pipecat-ai/pipecat"): [
                page([node(1), node(2)], total=3, cursor="1"),
                page([node(3)], total=3),
            ],
            pool_query("pipecat-ai/whisker"): [page([], total=0)],
        }
    )
    snapshot = await github.snapshot(["pipecat-ai/pipecat", "pipecat-ai/whisker"])

    assert [pr.number for pr in snapshot.pool["pipecat-ai/pipecat"]] == [1, 2, 3]
    assert snapshot.pool["pipecat-ai/whisker"] == []
    assert [pr.ref for pr in snapshot.assigned] == [PRRef("other/repo", 9)]
    # One request for the first pages, a second only for the repo with more.
    assert len(github.calls) == 2
    assert [k for k in github.calls[1] if k.startswith("q_")] == ["q_r0"]


async def test_snapshot_drops_failed_and_incomplete_searches():
    github = FakeGitHub(
        {
            ASSIGNED_QUERY: "rate limited",
            pool_query("pipecat-ai/pipecat"): [page([node(1)], total=2)],  # one missing
        }
    )
    snapshot = await github.snapshot(["pipecat-ai/pipecat"])
    assert snapshot.assigned is None
    assert snapshot.pool == {}


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
