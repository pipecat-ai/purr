"""What every Claude Agent SDK session Purr starts has in common."""

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from loguru import logger


def log_stderr(line: str) -> None:
    """Claude Code's stderr, into Purr's log.

    Without this, the SDK leaves Claude Code's stderr connected to your
    terminal, under Purr's screen: its warnings land on the UI, and the
    "show cursor" it prints on exit leaves the terminal's cursor blinking.
    """
    logger.debug(f"claude: {line.rstrip()}")


async def structured(
    notes: str, *, instructions: str, schema: dict, model: str | None, what: str
) -> dict:
    """One tool-less call that answers in ``schema``'s shape: the lead's plans, recaps,
    suggestions. Raises RuntimeError (saying no ``what`` came back) if it doesn't."""
    options = ClaudeAgentOptions(
        system_prompt=instructions,
        tools=[],
        setting_sources=[],
        output_format={"type": "json_schema", "schema": schema},
        model=model,
        stderr=log_stderr,
    )
    result = None
    async for message in query(prompt=notes, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None or result.is_error or not isinstance(result.structured_output, dict):
        raise RuntimeError(getattr(result, "result", None) or f"no {what} came back")
    return result.structured_output
