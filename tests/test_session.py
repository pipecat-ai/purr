from rich.console import Console

from purr.tui.session import render_entry


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
