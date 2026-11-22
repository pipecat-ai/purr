"""What every Claude Agent SDK session Purr starts has in common."""

from loguru import logger


def log_stderr(line: str) -> None:
    """Claude Code's stderr, into Purr's log.

    Without this, the SDK leaves Claude Code's stderr connected to your
    terminal, under Purr's screen: its warnings land on the UI, and the
    "show cursor" it prints on exit leaves the terminal's cursor blinking.
    """
    logger.debug(f"claude: {line.rstrip()}")
