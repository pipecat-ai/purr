"""Turning SDK messages and stored transcripts into transcript entries."""

from types import SimpleNamespace

from purr.review.agent import describe_tool_use, history_entries


def stored(type_: str, content) -> SimpleNamespace:
    return SimpleNamespace(type=type_, message={"role": type_, "content": content})


def test_history_entries_from_a_stored_transcript():
    messages = [
        stored("user", "Review pipecat-ai/pipecat#6924"),
        stored("assistant", [{"type": "thinking", "thinking": ""}]),
        stored(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Bash",
                    "input": {"command": "gh pr view 6924"},
                }
            ],
        ),
        stored(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "title: x"}],
                }
            ],
        ),
        stored("assistant", [{"type": "text", "text": "I recommend approving."}]),
    ]
    assert history_entries(messages) == [
        {"kind": "user", "text": "Review pipecat-ai/pipecat#6924"},
        {"kind": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr view 6924"}},
        {"kind": "tool_result", "tool_use_id": "t1", "is_error": False, "text": "title: x"},
        {"kind": "text", "text": "I recommend approving."},
    ]


def test_describe_tool_use():
    assert describe_tool_use("Bash", {"command": "git log"}) == "$ git log"
    assert describe_tool_use("Read", {"file_path": "a.py"}) == "Read a.py"
    assert describe_tool_use("mcp__purr__propose_action", {"description": "Approve"}) == (
        "Proposing: Approve"
    )
