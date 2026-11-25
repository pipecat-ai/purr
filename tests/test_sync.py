from dataclasses import replace

from purr.github import PRStatus, RemotePR, Snapshot
from purr.models import PR, GHState, PRChange, PRRef, PRState
from purr.sync import missing_reviewed, moved_reviewed, plan_sync

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
        assignees=kwargs.get("assignees", ()),
        requested=kwargs.get("requested", ()),
    )


def stored(number: int, repo: str = "pipecat-ai/pipecat", **kwargs) -> PR:
    return replace(remote(number, repo).to_pr("me"), **kwargs)


def changes(plan) -> list[tuple[int, PRChange]]:
    return [(c.pr.number, c.change) for c in plan.changes]


def test_new_pool_and_assigned_prs():
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": [remote(1)]},
        assigned=[remote(7, repo="other/repo")],
    )
    plan = plan_sync([], snapshot, watched=WATCHED)

    assert {(pr.number, pr.assigned_to_me) for pr in plan.upserts} == {(1, False), (7, True)}
    assert sorted(changes(plan)) == [(1, PRChange.POOLED), (7, PRChange.ASSIGNED)]


def test_first_sync_does_not_report_the_whole_pool():
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [remote(1), remote(2)]}, assigned=[])
    plan = plan_sync([], snapshot, watched=WATCHED, first_sync=WATCHED)

    assert len(plan.upserts) == 2
    assert plan.changes == []


def test_unchanged_prs_are_not_rewritten():
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [remote(1)]}, assigned=[])
    plan = plan_sync([stored(1)], snapshot, watched=WATCHED)
    assert plan.empty
    assert plan.changes == []


def test_assignment_changes():
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [remote(2)]}, assigned=[remote(1)])
    current = [stored(1), stored(2, assigned_to_me=True, state=PRState.DONE)]
    plan = plan_sync(current, snapshot, watched=WATCHED)

    assert sorted(changes(plan)) == [(1, PRChange.ASSIGNED), (2, PRChange.UNASSIGNED)]


def test_new_commits_only_matter_once_reviewed():
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": [remote(1, head_sha="bbb"), remote(2, head_sha="bbb")]},
        assigned=[],
    )
    current = [stored(1), stored(2, state=PRState.NEEDS_YOU)]
    plan = plan_sync(current, snapshot, watched=WATCHED)

    assert {pr.number for pr in plan.upserts} == {1, 2}
    assert changes(plan) == [(2, PRChange.NEW_COMMITS)]


def test_a_reviewed_prs_new_head_is_the_one_it_has():
    """The searches lag behind a push (Purr's own, say): the head from before it is no news,
    nor is a head that moved since; the head the PR has is."""
    snapshot = Snapshot(
        open={
            "pipecat-ai/pipecat": [
                remote(1, head_sha="aaa"),  # from before Purr pushed bbb
                remote(2, head_sha="ccc"),  # the author pushed
                remote(3, head_sha="ccc"),  # the author pushed twice: ddd now
                remote(4, head_sha="ccc"),  # never reviewed: as the search has it
                remote(5, head_sha="ccc"),  # couldn't be looked up: as the search has it
            ]
        },
        assigned=[],
    )
    reviewed = {"state": PRState.NEEDS_YOU, "head_sha": "bbb"}
    current = [
        stored(1, **reviewed),
        stored(2, **reviewed),
        stored(3, **reviewed),
        stored(4, head_sha="bbb"),
        stored(5, **reviewed),
    ]
    refs = {n: PRRef("pipecat-ai/pipecat", n) for n in range(1, 6)}
    assert moved_reviewed(current, snapshot) == [refs[n] for n in (1, 2, 3, 5)]

    statuses = {
        refs[1]: PRStatus(GHState.OPEN, "bbb"),
        refs[2]: PRStatus(GHState.OPEN, "ccc"),
        refs[3]: PRStatus(GHState.OPEN, "ddd"),
    }
    plan = plan_sync(current, snapshot, watched=WATCHED, statuses=statuses)
    assert sorted((pr.number, pr.head_sha) for pr in plan.upserts) == [
        (2, "ccc"),
        (3, "ddd"),
        (4, "ccc"),
        (5, "ccc"),
    ]
    assert sorted(changes(plan)) == [
        (2, PRChange.NEW_COMMITS),
        (3, PRChange.NEW_COMMITS),
        (5, PRChange.NEW_COMMITS),
    ]


def test_prs_leaving_the_pool_are_dropped_unless_reviewed():
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=[])
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
    plan = plan_sync(current, Snapshot(open={}, assigned=[]), watched=WATCHED)
    assert [ref.number for ref in plan.deletes] == [2]

    # The assigned search failed: nothing can be concluded about anything.
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=None)
    assert plan_sync(current, snapshot, watched=WATCHED).deletes == []
    assert missing_reviewed(current, snapshot, WATCHED) == []


def test_incremental_updates():
    snapshot = Snapshot(
        open={},
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

    # 5 wasn't updated, so it's left alone. 2 stays listed, with who took it.
    assert sorted(pr.number for pr in plan.upserts) == [1, 2, 3]
    assert plan.deletes == []
    assert next(pr for pr in plan.upserts if pr.number == 2).assignees == ("teammate",)
    assert sorted(changes(plan)) == [
        (2, PRChange.UNPOOLED),
        (3, PRChange.POOLED),
        (4, PRChange.CLOSED),
    ]


def test_prs_others_are_on_stay_listed():
    taken = remote(2, assignees=("alice",))
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [remote(1), taken]}, assigned=[], viewer="me")

    # Someone else's PR shows up quietly; a pool PR is news.
    plan = plan_sync([], snapshot, watched=WATCHED)
    assert sorted(pr.number for pr in plan.upserts) == [1, 2]
    assert changes(plan) == [(1, PRChange.POOLED)]
    assert [pr.others_on_it for pr in sorted(plan.upserts, key=lambda pr: pr.number)] == [
        False,
        True,
    ]


def test_pool_comings_and_goings():
    snapshot = Snapshot(
        open={
            "pipecat-ai/pipecat": [
                remote(1, assignees=("alice",)),  # taken by alice
                remote(2),  # alice let it go
                remote(3, assignees=("Me",)),  # assigned to you
            ]
        },
        assigned=[remote(3, assignees=("Me",))],
        viewer="me",
    )
    current = [stored(1), stored(2, assignees=("alice",)), stored(3)]
    plan = plan_sync(current, snapshot, watched=WATCHED)

    assert sorted(changes(plan)) == [
        (1, PRChange.UNPOOLED),
        (2, PRChange.POOLED),
        (3, PRChange.ASSIGNED),
    ]


def test_closed_prs_someone_else_had_go_quietly():
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=[], viewer="me")
    plan = plan_sync([stored(1, assignees=("alice",))], snapshot, watched=WATCHED)
    assert [ref.number for ref in plan.deletes] == [1]
    assert plan.changes == []


def test_review_requests_are_listed_and_news_but_start_nothing():
    asked = remote(5, repo="other/repo", requested=("me",))
    pool = remote(1, requested=("Me", "teammate"))
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": [pool]}, assigned=[], requested=[asked, pool], viewer="me"
    )
    plan = plan_sync([], snapshot, watched=WATCHED)
    assert {(pr.number, pr.requested_from_me, pr.assigned_to_me) for pr in plan.upserts} == {
        (1, True, False),
        (5, True, False),
    }
    assert sorted(changes(plan)) == [(1, PRChange.REQUESTED), (5, PRChange.REQUESTED)]

    # Asked on a PR already listed: news once.
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": [pool]}, assigned=[], requested=[pool], viewer="me"
    )
    assert changes(plan_sync([stored(1)], snapshot, watched=WATCHED)) == [(1, PRChange.REQUESTED)]
    already = [stored(1, requested_from_me=True)]
    assert plan_sync(already, snapshot, watched=WATCHED).changes == []


def test_review_requests_from_other_repos_come_and_go():
    current = [stored(5, repo="other/repo", requested_from_me=True)]
    # The requests search failed: nothing to conclude.
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=[], requested=None)
    assert plan_sync(current, snapshot, watched=WATCHED).deletes == []
    # It's no longer asked for (you reviewed it on GitHub, say), and Purr never did.
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=[], requested=[])
    plan = plan_sync(current, snapshot, watched=WATCHED)
    assert plan.deletes == [PRRef("other/repo", 5)]


def test_prs_you_took_part_in_are_tracked_quietly_and_stay_so():
    elsewhere = remote(8, repo="other/repo")
    pool = remote(1)
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": [pool]}, assigned=[], involved=[elsewhere, pool], viewer="me"
    )
    plan = plan_sync([stored(1)], snapshot, watched=WATCHED)
    assert {(pr.number, pr.involved) for pr in plan.upserts} == {(1, True), (8, True)}
    assert plan.changes == []  # not news; and other repos have no pool to join

    # Once you've taken part, you have, whatever a search says next.
    current = [stored(1, involved=True), stored(8, repo="other/repo", involved=True)]
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [pool]}, assigned=[], involved=[elsewhere])
    assert plan_sync(current, snapshot, watched=WATCHED).empty
    # Nothing is concluded from a failed search; a closed PR drops out (never reviewed).
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [pool]}, assigned=[], involved=None)
    assert plan_sync(current, snapshot, watched=WATCHED).deletes == []
    snapshot = Snapshot(open={"pipecat-ai/pipecat": [pool]}, assigned=[], involved=[])
    assert plan_sync(current, snapshot, watched=WATCHED).deletes == [PRRef("other/repo", 8)]


def test_your_own_prs_are_tracked_quietly():
    mine = replace(remote(5), author="me")  # the repo's search leaves it out: it's yours
    assigned = replace(mine, assignees=("me",))  # authors often assign themselves
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": []}, assigned=[assigned], mine=[mine], viewer="me"
    )
    plan = plan_sync([], snapshot, watched=WATCHED)
    [pr] = plan.upserts
    assert pr.authored and pr.assigned_to_me and not pr.in_pool
    assert plan.changes == []  # you opened it and assigned yourself: not news

    # Pushing to it, or assigning yourself later: not news either.
    old = replace(pr, assigned_to_me=False, assignees=(), state=PRState.DONE)
    pushed = replace(assigned, head_sha="bbb")
    snapshot = Snapshot(
        open={"pipecat-ai/pipecat": []}, assigned=[pushed], mine=[pushed], viewer="me"
    )
    assert plan_sync([old], snapshot, watched=WATCHED).changes == []

    # Its search failed: nothing's concluded about it.
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=[], mine=None, viewer="me")
    assert plan_sync([pr], snapshot, watched=WATCHED).deletes == []
    # Merged, say: out of every search, so out of the list, quietly.
    snapshot = Snapshot(open={"pipecat-ai/pipecat": []}, assigned=[], mine=[], viewer="me")
    plan = plan_sync([pr], snapshot, watched=WATCHED)
    assert plan.deletes == [pr.ref] and plan.changes == []
