"""Tests never reach real Claude: every way Purr calls it raises instead."""

import pytest


def _no_claude(*args, **kwargs):
    raise AssertionError("A test tried to call the real Claude; use the fakes in tests/fakes.py")


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    import purr.lead
    import purr.recap
    import purr.review.agent

    monkeypatch.setattr(purr.lead, "query", _no_claude)
    monkeypatch.setattr(purr.recap, "query", _no_claude)
    monkeypatch.setattr(purr.review.agent, "ClaudeSDKClient", _no_claude)
