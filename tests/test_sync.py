from dataclasses import replace

from purr.github import PRStatus, RemotePR, Snapshot
from purr.models import PR, GHState, PRChange, PRRef, PRState
from purr.sync import missing_reviewed, plan_sync

WATCHED = ["pipecat-ai/pipecat"]


def remote(number: int, repo: str = "pipecat-ai/pipecat", **kwargs) -> RemotePR:
    return RemotePR(
        repo=repo,
        number=number,
        title=kwargs.get("title", f"PR {number}"),
        author="someone",
        url=f"https://github.com/{repo}/pull/{number}",
        head_sha=kwargs.get("head_sha", "aaa"),
        updated_at="2026-11-20T10:00:00Z",
    )


def stored(number: int, repo: str = "pipecat-ai/pipecat", **kwargs) -> PR:
    return replace(remote(number, repo).to_pr(assigned_to_me=False), **kwargs)


def changes(plan) -> list[tuple[int, PRChange]]:
    return [(c.pr.number, c.change) for c in plan.changes]


def test_new_pool_and_assigned_prs():
    snapshot = Snapshot(
        pool={"pipecat-ai/pipecat": [remote(1)]},
        assigned=[remote(7, repo="other/repo")],
    )
    plan = plan_sync([], snapshot, watched=WATCHED)

    assert {(pr.number, pr.assigned_to_me) for pr in plan.upserts} == {(1, False), (7, True)}
    assert sorted(changes(plan)) == [(1, PRChange.POOLED), (7, PRChange.ASSIGNED)]


def test_first_sync_does_not_report_the_whole_pool():
    snapshot = Snapshot(pool={"pipecat-ai/pipecat": [remote(1), remote(2)]}, assigned=[])
    plan = plan_sync([], snapshot, watched=WATCHED, first_sync=WATCHED)

    assert len(plan.upserts) == 2
    assert plan.changes == []


def test_unchanged_prs_are_not_rewritten():
    snapshot = Snapshot(pool={"pipecat-ai/pipecat": [remote(1)]}, assigned=[])
    plan = plan_sync([stored(1)], snapshot, watched=WATCHED)
    assert plan.empty
    assert plan.changes == []


def test_assignment_changes():
    snapshot = Snapshot(pool={"pipecat-ai/pipecat": [remote(2)]}, assigned=[remote(1)])
    current = [stored(1), stored(2, assigned_to_me=True, state=PRState.DONE)]
    plan = plan_sync(current, snapshot, watched=WATCHED)

    assert sorted(changes(plan)) == [(1, PRChange.ASSIGNED), (2, PRChange.UNASSIGNED)]


def test_new_commits_only_matter_once_reviewed():
    snapshot = Snapshot(
        pool={"pipecat-ai/pipecat": [remote(1, head_sha="bbb"), remote(2, head_sha="bbb")]},
        assigned=[],
    )
    current = [stored(1), stored(2, state=PRState.NEEDS_YOU)]
    plan = plan_sync(current, snapshot, watched=WATCHED)

    assert {pr.number for pr in plan.upserts} == {1, 2}
    assert changes(plan) == [(2, PRChange.NEW_COMMITS)]


def test_prs_leaving_the_pool_are_dropped_unless_reviewed():
    snapshot = Snapshot(pool={"pipecat-ai/pipecat": []}, assigned=[])
    current = [
        stored(1),
        stored(2, assigned_to_me=True),
        stored(3, state=PRState.NEEDS_YOU),
        stored(4, state=PRState.DONE, assigned_to_me=True),
        stored(5, state=PRState.DONE),
    ]
    assert missing_reviewed(current, snapshot, WATCHED) == [
        PRRef("pipecat-ai/pipecat", n) for n in (3, 4, 5)
    ]

    statuses = {
        PRRef("pipecat-ai/pipecat", 3): PRStatus(GHState.MERGED, "aaa"),
        PRRef("pipecat-ai/pipecat", 4): PRStatus(GHState.OPEN, "aaa"),
        # 5 couldn't be looked up
    }
    plan = plan_sync(current, snapshot, watched=WATCHED, statuses=statuses)

    assert sorted(ref.number for ref in plan.deletes) == [1, 2]
    assert plan.gh_states == {PRRef("pipecat-ai/pipecat", 3): GHState.MERGED}
    assert [(pr.number, pr.assigned_to_me) for pr in plan.upserts] == [(4, False)]
    assert sorted(changes(plan)) == [
        (1, PRChange.UNPOOLED),
        (2, PRChange.UNASSIGNED),
        (3, PRChange.MERGED),
        (4, PRChange.UNASSIGNED),
    ]


def test_nothing_is_concluded_from_failed_searches():
    current = [stored(1), stored(2, repo="other/repo", assigned_to_me=True)]

    # The repo's pool search failed: its PRs stay.
    plan = plan_sync(current, Snapshot(pool={}, assigned=[]), watched=WATCHED)
    assert [ref.number for ref in plan.deletes] == [2]

    # The assigned search failed: nothing can be concluded about anything.
    snapshot = Snapshot(pool={"pipecat-ai/pipecat": []}, assigned=None)
    assert plan_sync(current, snapshot, watched=WATCHED).deletes == []
    assert missing_reviewed(current, snapshot, WATCHED) == []


def test_incremental_updates():
    snapshot = Snapshot(
        pool={},
        assigned=[],
        updates={
            "pipecat-ai/pipecat": [
                remote(1, title="Renamed"),  # still in the pool
                replace(remote(2), assignees=("teammate",)),  # taken by someone
                remote(3),  # new
                replace(remote(4), state=GHState.CLOSED),  # reviewed, closed
            ]
        },
        viewer="me",
    )
    current = [stored(1), stored(2), stored(4, state=PRState.DONE), stored(5)]

    assert missing_reviewed(current, snapshot, WATCHED) == [PRRef("pipecat-ai/pipecat", 4)]
    statuses = {PRRef("pipecat-ai/pipecat", 4): PRStatus(GHState.CLOSED, "aaa")}
    plan = plan_sync(current, snapshot, watched=WATCHED, statuses=statuses)

    # 5 wasn't updated, so it's left alone.
    assert sorted(pr.number for pr in plan.upserts) == [1, 3]
    assert [ref.number for ref in plan.deletes] == [2]
    assert sorted(changes(plan)) == [
        (2, PRChange.UNPOOLED),
        (3, PRChange.POOLED),
        (4, PRChange.CLOSED),
    ]
