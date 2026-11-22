"""Turning SDK messages and stored transcripts into transcript entries."""

import io
from types import SimpleNamespace

from rich.console import Console

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


def test_guidelines_section():
    from purr.review.agent import guidelines_section

    assert guidelines_section("a/b", "") == ""
    section = guidelines_section("a/b", "### AGENTS.md\n\nShort functions.")
    assert "a/b's own guidelines" in section and "Short functions." in section


def test_a_skill_started_as_a_slash_command_shows_as_typed():
    command = (
        "<command-message>pipecat:code-review</command-message>\n"
        "<command-name>/pipecat:code-review</command-name>\n"
        "<command-args>https://github.com/pipecat-ai/pipecat/pull/2841</command-args>"
    )
    assert history_entries([stored("user", command)]) == [
        {
            "kind": "user",
            "text": "/pipecat:code-review https://github.com/pipecat-ai/pipecat/pull/2841",
        }
    ]


def test_subagent_entries_say_which_agent():
    from claude_agent_sdk import AssistantMessage, ToolUseBlock, UserMessage

    from purr.review.agent import transcript_entries
    from purr.tui.session import render_entry

    call = ToolUseBlock(id="t2", name="Read", input={"file_path": "bus.py"})
    inside = AssistantMessage(content=[call], model="m", parent_tool_use_id="t1")
    prompt = UserMessage(content="Scan for obvious bugs.", parent_tool_use_id="t1")
    entries = transcript_entries(inside) + transcript_entries(prompt)
    assert [(e["kind"], e["agent"]) for e in entries] == [("tool_use", "t1"), ("user", "t1")]
    # Recorded, but the PR's conversation only shows the call that started the subagent.
    assert all(render_entry(e) is None for e in entries)
    started = {"kind": "tool_use", "name": "Agent", "input": {"description": "Bug scan"}}
    console = Console(width=80, record=True, file=io.StringIO())
    console.print(render_entry(started))
    assert "Agent(Bug scan)" in console.export_text()
    assert describe_tool_use("Agent", {"description": "Bug scan", "prompt": "…"}) == (
        "Agent Bug scan"
    )


async def test_claude_codes_stderr_never_reaches_your_terminal(monkeypatch, tmp_path):
    """Left to the SDK, it goes to the terminal under Purr's screen (and shows the cursor)."""
    import pytest

    import purr.lead
    import purr.recap
    import purr.review.agent
    from purr.claude import log_stderr
    from purr.models import PR

    seen = []

    class Stop(Exception):
        pass

    def query(prompt, options):
        seen.append(options.stderr)
        raise Stop

    class Client:
        def __init__(self, options):
            seen.append(options.stderr)
            raise Stop

    monkeypatch.setattr(purr.lead, "query", query)
    monkeypatch.setattr(purr.recap, "query", query)
    monkeypatch.setattr(purr.review.agent, "ClaudeSDKClient", Client)
    pr = PR(repo="a/b", number=1, title="t", author="x", url="u", head_sha="h", updated_at="")
    with pytest.raises(Stop):
        await purr.lead.ClaudeLead().plan("notes")
    with pytest.raises(Stop):
        await purr.recap.ClaudeRecapAgent().summarize("notes")
    with pytest.raises(Stop):
        await purr.review.agent.ClaudeReviewAgent().run(
            pr=pr, worktree=tmp_path, session_id="s", resume=False, prompt="p", callbacks=None
        )
    assert seen == [log_stderr, log_stderr, log_stderr]
