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
