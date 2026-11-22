"""Copying what you select to your clipboard.

Textual copies by asking the terminal (OSC 52), which works over ssh too, but
not every terminal takes it: GNOME Terminal and the others built on VTE ignore
it on purpose. So Purr also uses the system's copy command when there's one.
"""

import asyncio
import os
import shutil
import sys


def copy_command() -> list[str] | None:
    """The system's copy command, if it has one Purr can use."""
    if sys.platform == "darwin":
        return ["pbcopy"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"]
    if os.environ.get("DISPLAY"):
        if shutil.which("xclip"):
            return ["xclip", "-selection", "clipboard"]
        if shutil.which("xsel"):
            return ["xsel", "--clipboard", "--input"]
    return None


def terminal_copies() -> bool:
    """Whether the terminal takes copies from the apps in it (VTE ones don't)."""
    return "VTE_VERSION" not in os.environ


async def system_copy(command: list[str], text: str) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            # wl-copy and xclip stay behind to serve the clipboard: don't wait on them.
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.communicate(text.encode())
    except OSError:
        pass
