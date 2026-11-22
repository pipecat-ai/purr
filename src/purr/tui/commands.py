"""Slash commands in a PR's prompt.

Purr's own are handled by Purr and never reach the agent. Anything else that
starts with ``/`` goes to the PR's Claude Code session as typed: Claude Code's
commands that make sense here (``/compact``, ``/context``), the repo's skills
(``/pipecat:prose-review``), and your own.
"""

from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class Command:
    name: str
    args: str
    help: str


PURR_COMMANDS = [
    Command("help", "", "What you can type here"),
    Command(
        "review", "[quick|full]", "Start over: a fresh review, planned by the lead unless you say"
    ),
    Command("clear", "[quick|full]", "Same as /review: a fresh review, in a new conversation"),
    Command("model", "[name|default]", "The model for this PR's conversation (it sticks)"),
    Command("cost", "", "What this PR's reviews and questions cost, and how long they took"),
    Command("shell", "", "A shell in the PR's worktree, to run things yourself (same as !)"),
    Command("claude", "", "This conversation in Claude Code itself, for hands-on work (same as c)"),
]
# Claude Code's own, worth having here; they go to the session as typed.
CLAUDE_CODE_COMMANDS = [
    Command("compact", "", "Summarize the conversation so far, to keep going in a long one"),
    Command("context", "", "What's taking up the conversation's context"),
]
PURR_NAMES = {command.name for command in PURR_COMMANDS}


def parse(text: str) -> tuple[str, str] | None:
    """``/name args`` as ``(name, args)``; None if it isn't a command."""
    if not text.startswith("/") or text.startswith("//"):
        return None
    name, _, args = text[1:].partition(" ")
    return (name, args.strip()) if name else None


def suggestions(text: str, skills: list[dict]) -> list[Command]:
    """Commands that start with what's typed (``/re``): Purr's, Claude Code's, the repo's skills."""
    if not text.startswith("/") or " " in text or "\n" in text:
        return []
    typed = text[1:].lower()
    everything = [
        *PURR_COMMANDS,
        *CLAUDE_CODE_COMMANDS,
        *(Command(str(s["name"]), "", str(s.get("description", ""))) for s in skills),
    ]
    starts = [c for c in everything if c.name.lower().startswith(typed)]
    # pipecat:prose-review comes up for /prose too, once there's enough to go on.
    inside = [
        c for c in everything if c not in starts and len(typed) >= 3 and typed in c.name.lower()
    ]
    return starts + inside


def render_help(skills: list[dict]) -> RenderableType:
    def table(commands: list[Command]) -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column()
        for command in commands:
            # Text, not markup: "[quick|full]" would read as a style.
            usage = Text(f"/{command.name} {command.args}".strip(), style="bold")
            grid.add_row(usage, Text(command.help, style="dim"))
        return grid

    parts: list[RenderableType] = [
        Text("Purr's commands", style="bold magenta"),
        table(PURR_COMMANDS),
        Text("\nPassed to Claude Code", style="bold magenta"),
        table(CLAUDE_CODE_COMMANDS),
    ]
    if skills:
        parts += [
            Text("\nThe repo's skills", style="bold magenta"),
            table([Command(str(s["name"]), "", str(s.get("description", ""))) for s in skills]),
        ]
    parts.append(
        Text(
            "\nAnything else after / goes to Claude Code as you typed it (your own skills too). "
            "Keys: ! a shell in the worktree, c Claude Code, d the diff.",
            style="dim",
        )
    )
    return Group(*parts)


def render_cost(runs: list[dict]) -> RenderableType:
    """This PR's runs: what each was, when, how long, what it cost."""
    if not runs:
        return Text("Nothing has run on this PR yet.", style="dim")
    grid = Table(box=None, padding=(0, 2), show_edge=False, header_style="dim")
    for name, justify in [
        ("run", "left"),
        ("started", "left"),
        ("took", "right"),
        ("turns", "right"),
        ("tokens out", "right"),
        ("cost", "right"),
        ("model", "left"),
    ]:
        grid.add_column(name, justify=justify, no_wrap=True)
    total_cost, total_ms = 0.0, 0
    for run in runs:
        cost = run.get("cost_usd") or 0.0
        took = run.get("duration_ms") or 0
        total_cost += cost
        total_ms += took
        status = "" if run.get("status") in (None, "finished") else f" ({run['status']})"
        grid.add_row(
            f"{run.get('kind', '')}{status}",
            str(run.get("started_at") or run.get("queued_at") or "")[5:16].replace("T", " "),
            f"{took / 1000:.0f}s" if took else "-",
            str(run.get("turns") or "-"),
            f"{run.get('output_tokens') or 0:,}",
            f"${cost:.2f}" if cost else "-",
            str(run.get("model") or ""),
        )
    total = Text(f"{len(runs)} runs · {total_ms / 60000:.1f} min · ${total_cost:.2f}", style="bold")
    return Group(grid, total)
