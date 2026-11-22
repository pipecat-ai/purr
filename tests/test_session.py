from rich.console import Console

from purr.models import Reply
from purr.tui.session import render_entry, render_replies, replied


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
    from purr.tui.screens import OTHERS, REQUESTED, group

    pr = PR("a/b", 1, "Fix", "someone", "u", "abc", "t", requested_from_me=True)
    assert group(pr) == REQUESTED
    assert group(replace(pr, assignees=("alice",))) == REQUESTED  # asked of you all the same
    assert group(replace(pr, requested_from_me=False, assignees=("alice",))) == OTHERS
    assert group(replace(pr, assigned_to_me=True, assignees=("me",))) == PRState.NOT_REVIEWED
    assert group(replace(pr, state=PRState.DONE)) == PRState.DONE
