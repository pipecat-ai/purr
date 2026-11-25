"""Opening a page (a PR on GitHub) in your browser, from the terminal Purr's in."""

import os
import shutil
import subprocess
import sys
import webbrowser


def open_page(url: str) -> None:
    """Open ``url`` in your browser, without waiting for it.

    Through the system's opener (``open``, ``xdg-open``) with its output thrown away:
    a browser starting up says things on the terminal, over Purr's screen. Python's
    ``webbrowser`` when there's no opener.
    """
    if sys.platform == "win32":
        os.startfile(url)  # noqa: S606 (a URL, for the shell to open)
        return
    opener = shutil.which("open" if sys.platform == "darwin" else "xdg-open")
    if not opener:
        webbrowser.open(url)
        return
    subprocess.Popen(
        [opener, url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # not Purr's: ctrl+c, or Purr quitting, leaves it be
    )
