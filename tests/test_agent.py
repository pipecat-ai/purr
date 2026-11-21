"""Turning SDK messages and stored transcripts into transcript entries."""

from types import SimpleNamespace

from purr.review.agent import describe_tool_use, history_entries


def stored(type_: str, content) -> SimpleNamespace:
    return SimpleNamespace(type=type_, message={"role": type_, "content": content})


def test_history_entries_from_a_stored_transcript():
    tool_use = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr view"}}
    text = [{"type": "text", "text": "x"}]
    result = {"type": "tool_result", "tool_use_id": "t1", "content": text}
    messages = [
        stored("user", "Review pipecat-ai/pipecat#6924"),
        stored("assistant", [{"type": "thinking", "thinking": ""}]),
        stored("assistant", [{"type": "thinking", "thinking": "Check the lock file first."}]),
        stored("assistant", [tool_use]),
        stored("user", [result]),
        stored("assistant", [{"type": "text", "text": "I recommend approving."}]),
    ]
    assert history_entries(messages) == [
        {"kind": "user", "text": "Review pipecat-ai/pipecat#6924"},
        {"kind": "thinking", "text": "Check the lock file first."},
        {"kind": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr view"}},
        {"kind": "tool_result", "tool_use_id": "t1", "is_error": False, "text": "x"},
        {"kind": "text", "text": "I recommend approving."},
    ]


def test_describe_tool_use():
    assert describe_tool_use("Bash", {"command": "git log"}) == "$ git log"
    assert describe_tool_use("Read", {"file_path": "a.py"}) == "Read a.py"
    assert describe_tool_use("mcp__purr__propose_action", {"description": "Approve"}) == (
        "Proposing: Approve"
    )


def test_claude_code_command_resumes_safely():
    from purr.review.agent import claude_code_command

    command = claude_code_command("1234", "pipecat-ai/pipecat#2841")
    assert command[:3] == ["claude", "--resume", "1234"]
    assert command[command.index("--setting-sources") + 1] == "user"
    assert "--fork-session" not in command  # same session ID, so Purr can pick it up again
    disallowed = command[command.index("--disallowedTools") + 1 :]
    assert "Bash(git push:*)" in disallowed and "Bash(gh pr merge:*)" in disallowed


def test_live_thinking_becomes_an_entry():
    from claude_agent_sdk import AssistantMessage, TextBlock, ThinkingBlock

    from purr.review.agent import transcript_entries

    message = AssistantMessage(
        content=[
            ThinkingBlock(thinking="", signature="s"),
            ThinkingBlock(thinking="The lock bump looks deliberate.", signature="s"),
            TextBlock(text="Looks good."),
        ],
        model="m",
    )
    assert transcript_entries(message) == [
        {"kind": "thinking", "text": "The lock bump looks deliberate."},
        {"kind": "text", "text": "Looks good."},
    ]
