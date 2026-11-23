"""What every Claude Agent SDK session Purr starts has in common, and the Claude Code
you run yourself.

The Agent SDK comes with its own Claude Code, which it runs reviews on: you don't need
to install it. You do need to be logged in (to Claude, or with ANTHROPIC_API_KEY);
``purr --login`` does it with the same Claude Code.
"""

import platform
import shutil
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query
from loguru import logger

# What to do when there's no Claude Code to run (it should come with the Agent SDK).
NO_CLAUDE_CODE = "Install it: curl -fsSL https://claude.ai/install.sh | bash"


def claude_cli() -> str | None:
    """The Claude Code to run yourself (``c``, ``purr --login``): yours, if it's installed;
    else the one the Agent SDK comes with, which reviews run on. None: neither."""
    bundled = bundled_claude()
    return shutil.which("claude") or (str(bundled) if bundled.exists() else None)


def bundled_claude() -> Path:
    name = "claude.exe" if platform.system() == "Windows" else "claude"
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / name


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
