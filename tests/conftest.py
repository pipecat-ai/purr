"""Tests never reach real Claude (every way Purr calls it raises instead), nor your own
Purr config and data (they point at a temporary directory instead), nor your browser
(pages Purr opens are kept in ``opened`` instead)."""

import pytest
import textual._wait

# After each key a test presses, Textual waits for the app to go idle: under 5% CPU in a
# window of 20 ms, twice. Most of the TUI tests' time went there; 5 ms windows, with the
# same 5%, make them about twice as fast.
textual._wait.SLEEP_GRANULARITY = 0.005
textual._wait.SLEEP_IDLE = textual._wait.SLEEP_GRANULARITY / 20


def _no_claude(*args, **kwargs):
    raise AssertionError("A test tried to call the real Claude; use the fakes in tests/fakes.py")


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    import purr.claude
    import purr.review.agent

    # The lead's plans, recaps and suggestions all go through purr.claude.structured.
    monkeypatch.setattr(purr.claude, "query", _no_claude)
    monkeypatch.setattr(purr.review.agent, "ClaudeSDKClient", _no_claude)


@pytest.fixture(autouse=True)
def no_real_config(monkeypatch, tmp_path_factory):
    home = tmp_path_factory.mktemp("purr-home")
    monkeypatch.setenv("PURR_CONFIG_DIR", str(home / "config"))
    monkeypatch.setenv("PURR_DATA_DIR", str(home / "data"))


@pytest.fixture(autouse=True)
def opened(monkeypatch) -> list[str]:
    """The pages Purr opened in your browser, as it would have."""
    import purr.tui.browser

    pages: list[str] = []
    monkeypatch.setattr(purr.tui.browser, "open_page", pages.append)
    return pages
