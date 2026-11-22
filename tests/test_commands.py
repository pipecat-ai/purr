"""Slash commands in a PR's prompt: what's Purr's, what's suggested."""

import io

from rich.console import Console

from purr.tui.commands import parse, render_cost, render_help, suggestions

SKILLS = [
    {"name": "pipecat:prose-review", "description": "Check the prose"},
    {"name": "pipecat:docstring", "description": "Document a module"},
]


def plain(renderable) -> str:
    console = Console(width=120, record=True, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def test_parse():
    assert parse("/review quick") == ("review", "quick")
    assert parse("/cost") == ("cost", "")
    assert parse("/pipecat:prose-review src/a.py") == ("pipecat:prose-review", "src/a.py")
    assert parse("why /this?") is None
    assert parse("//not a command") is None and parse("/") is None


def test_suggestions():
    names = lambda text: [c.name for c in suggestions(text, SKILLS)]  # noqa: E731
    assert names("/co") == ["cost", "compact", "context"]
    assert names("/prose") == ["pipecat:prose-review"]  # by what's in the name, too
    assert names("/r") == ["review"]  # but not from one letter
    assert names("/pipecat:d") == ["pipecat:docstring"]
    assert "help" in names("/") and "pipecat:docstring" in names("/")
    assert names("/cost ") == [] and names("hello") == [] and names("/a\nb") == []


def test_help_and_cost():
    text = plain(render_help(SKILLS))
    assert "/review [quick|full]" in text and "/compact" in text
    assert "/pipecat:prose-review" in text and "Check the prose" in text

    assert "Nothing has run" in plain(render_cost([]))
    runs = [
        {"kind": "review", "status": "finished", "started_at": "2026-11-21T10:00:00.000Z",
         "duration_ms": 90_000, "turns": 12, "output_tokens": 4321, "cost_usd": 1.25,
         "model": "claude-opus-5-5"},
        {"kind": "message", "status": "failed", "started_at": "2026-11-21T10:05:00.000Z",
         "duration_ms": 30_000, "cost_usd": 0.1},
    ]  # fmt: skip
    text = plain(render_cost(runs))
    assert "11-21 10:00" in text and "90s" in text and "4,321" in text and "$1.25" in text
    assert "message (failed)" in text
    assert "2 runs · 2.0 min · $1.35" in text
