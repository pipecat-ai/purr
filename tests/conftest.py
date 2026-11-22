"""Tests never reach real Claude (every way Purr calls it raises instead), nor your own
Purr config and data (they point at a temporary directory instead)."""

import pytest


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
