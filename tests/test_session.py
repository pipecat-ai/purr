from rich.console import Console

from purr.messages import PermissionRequest
from purr.models import PR, ActionKind, DraftComment, Reply
from purr.tui.session import (
    SessionState,
    needs_you_for,
    render_entry,
    render_replies,
    replied,
)


def rendered(entry) -> str:
    console = Console(width=100, record=True)
    console.print(render_entry(entry))
    return console.export_text()


def test_a_proposal_shows_everything_that_would_be_posted():
    text = rendered(
        {
            "kind": "tool_use",
            "name": "mcp__purr__propose_action",
            "input": {
                "kind": "request_changes",
                "description": "Post 1 comment and request changes",
                "body": "One race to fix.",
                "comments": [{"path": "bus.py", "line": 42, "body": "Race here"}],
                "replies": [{"thread_id": "T1", "body": "Still there."}],
            },
        }
    )
    assert "Proposed: Post 1 comment and request changes" in text
    assert "Request changes" in text
    assert "One race to fix." in text
    assert "bus.py:42  Race here" in text
    assert "reply in thread  Still there." in text


def test_paths_in_tool_calls_are_relative_to_the_worktree():
    text = rendered(
        {
            "kind": "tool_use",
            "name": "Read",
            "input": {"file_path": "/data/worktrees/pipecat-ai/pipecat/12/src/bus.py"},
        }
    )
    assert "Read(src/bus.py)" in text


def test_reasoning_is_shown():
    assert "Check the lock file first." in rendered(
        {"kind": "thinking", "text": "Check the lock file first."}
    )


def test_replies_say_who_and_where():
    alice = Reply("alice", "Fixed it,\n  see the test.", "t1", where="bus.py:42")
    bob = Reply("bob", "Why?", "t2")
    assert replied([alice]) == "alice replied"
    assert replied([alice, bob, Reply("alice", "Ping", "t3")]) == "alice and bob replied (3)"
    text = render_replies([alice, bob]).plain
    assert text.splitlines() == [
        "alice and bob replied (2):",
        "  alice · bus.py:42: Fixed it, see the test.",
        "  bob: Why?",
        "[x] no reply needed   or answer: here (ask the agent) or on GitHub",
    ]


def test_prs_asking_for_your_review_have_their_own_group():
    from dataclasses import replace

    from purr.models import PR, PRState
    from purr.tui.lists import OTHERS, REQUESTED, group

    pr = PR("a/b", 1, "Fix", "someone", "u", "abc", "t", requested_from_me=True)
    assert group(pr) == REQUESTED
    assert group(replace(pr, assignees=("alice",))) == REQUESTED  # asked of you all the same
    assert group(replace(pr, requested_from_me=False, assignees=("alice",))) == OTHERS
    assert group(replace(pr, assigned_to_me=True, assignees=("me",))) == PRState.NOT_REVIEWED
    assert group(replace(pr, state=PRState.DONE)) == PRState.DONE


def test_a_says_what_it_does():
    state = SessionState()
    assert state.a_does() == ""  # nothing for it to do
    state.verdict_due = True  # nothing proposed: it waits for your verdict alone
    assert state.a_does() == "Approve PR"
    state.mine = [DraftComment(1, "bus.py", 3, "RIGHT", "Why?", "abc")]
    assert state.a_does() == "Post comments"
    said = {
        ActionKind.COMMENT: "Post comment",
        ActionKind.APPROVE: "Approve PR",
        ActionKind.REQUEST_CHANGES: "Request changes",
        ActionKind.PUSH: "Push",
    }
    for kind, label in said.items():
        state.proposal = kind
        assert state.a_does() == label
    state.progress = "Thinking"  # the agent's at it: what it says may change the proposal
    assert state.a_does() == ""
    state.ask = PermissionRequest(
        source="reviews", pr="a/b#1", request_id="r", tool="Bash", description="", reason=""
    )
    assert state.a_does() == "Allow once"  # the agent's question comes first


def test_what_a_pr_needs_you_for():
    pr = PR("a/b", 1, "Fix", "x", "u", "abc", "2026-11-20T10:00:00Z")
    ask = PermissionRequest(
        source="reviews", pr="a/b#1", request_id="r", tool="Bash",
        description="$ uv run pytest", reason="",
    )  # fmt: skip

    def needs(ask=None, proposal=None, drafted=False, failed=False, replies=()) -> str:
        on = PR("a/b", 1, "Fix", "x", "u", "abc", "2026-11-20T10:00:00Z", replies=replies)
        return needs_you_for(on, ask, proposal, drafted=drafted, failed=failed)

    alice = (Reply("alice", "Why?", "2026-11-21T10:00:00Z"),)
    # The most pressing first, as the decision box asks.
    assert needs(ask, ActionKind.APPROVE, replies=alice) == "Asks: $ uv run pytest"
    assert needs(None, ActionKind.APPROVE, replies=alice) == "Approve PR?"
    assert needs(None, ActionKind.REQUEST_CHANGES) == "Request changes?"
    assert needs(drafted=True, replies=alice) == "Post your comments?"
    assert needs(replies=alice, failed=True) == "alice replied"
    assert needs(failed=True) == "Review failed"
    assert needs() == "No verdict yet"
    assert pr.replies == ()


def test_the_merge_box_lists_checks_while_ci_runs():
    from datetime import UTC, datetime

    from purr.merging import concerns
    from purr.messages import MergeReadiness
    from purr.models import Check, CheckState, MergeCheck
    from purr.tui.session import render_merge

    pr = PR("a/b", 7, "Fix", "x", "u", "abc", "2026-11-20T10:00:00Z")
    checks = (
        *(Check(f"Job {i}", CheckState.PASSED) for i in range(12)),
        Check("Docs", CheckState.FAILED),
        Check("Tests", CheckState.RUNNING, started="2026-11-25T10:00:00Z"),
    )
    check = MergeCheck(
        head_sha="abc", base="main", method="merge", checks=checks, assignees=("me", "alice")
    )
    readiness = MergeReadiness(check=check, blockers=[], concerns=concerns(check, "me"))
    shown = str(render_merge(pr, readiness, now=datetime(2026, 11, 25, 10, 2, 5, tzinfo=UTC)))
    # Too many to list: those that failed or still run, and how many more passed.
    assert "✗ Docs" in shown and "Tests  2m 5s" in shown and "✓ 12 more passed" in shown
    assert "Job 3" not in shown
    # Listed, they aren't concerns again; the rest are.
    assert "Docs failed" not in shown and "still running" not in shown
    assert "⚠ alice hasn't approved" in shown


def test_what_you_approved_and_is_still_open_is_to_merge():
    from dataclasses import replace

    from purr.models import PR, GHState, PRState, Verdict
    from purr.tui.lists import TO_MERGE, YOURS, group

    pr = PR("a/b", 1, "Fix", "someone", "u", "abc", "t", state=PRState.DONE)
    approved = replace(pr, verdict=Verdict(ActionKind.APPROVE, "abc"))
    assert group(approved) == TO_MERGE  # merging's next: in Done, it'd be missed
    assert group(pr) == PRState.DONE  # done without a verdict (a comment)
    assert group(replace(pr, verdict=Verdict(ActionKind.REQUEST_CHANGES, "abc"))) == PRState.DONE
    assert group(replace(approved, verdict=Verdict(ActionKind.APPROVE, "old"))) == PRState.DONE
    assert group(replace(approved, gh_state=GHState.MERGED)) == PRState.DONE
    assert group(replace(approved, authored=True)) == YOURS


def test_review_requests_come_before_up_next():
    from purr.tui.lists import GROUPS, REQUESTED, UP_NEXT

    # Someone asked you (a teammate, mostly, whose PRs come first): before Purr's picks.
    keys = [key for key, *_ in GROUPS]
    assert keys.index(REQUESTED) < keys.index(UP_NEXT)
