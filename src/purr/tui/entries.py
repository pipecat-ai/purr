"""How a PR session's transcript entries show: as Claude Code shows them."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from purr.review.agent import INSTEAD, thought_headline
from purr.review.tools import PROPOSE_ACTION, PURR_TOOL_PREFIX, SUBMIT_REVIEW
from purr.tui.theme import palette

_SEVERITY_STYLE = {"issue": "bold red", "suggestion": "yellow", "nit": "dim"}
# A background task's event: its mark, what it says, its style.
_TASK_EVENTS = {
    "started": ("◷", "started", "dim"),
    "completed": ("✓", "done", "dim green"),
    "stopped": ("◼", "stopped", "dim"),
    "killed": ("◼", "stopped", "dim"),
}
# Absolute paths into a PR's worktree read better relative to it.
_WORKTREE_PREFIX = re.compile(r"\S*/worktrees/[^/\s]+/[^/\s]+/\d+/")


def _bullet(body: RenderableType, style: str = "") -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_row(Text("⏺", style=style or "white"), body)
    return grid


def short(value: str, width: int = 100) -> str:
    """On one line, at most ``width`` long, paths in a PR's worktree relative to it."""
    value = _WORKTREE_PREFIX.sub("", value).replace("\n", " ⏎ ")
    return value if len(value) <= width else value[: width - 1] + "…"


_VERDICT = {
    "request_changes": "Request changes",
    "approve": "Approve",
    "comment": "Comment",
    "push": "Push",
}


def render_proposal(proposal: dict) -> RenderableType:
    """A proposed review, in full: what would be posted if the user approves."""
    parts: list[RenderableType] = [
        Text(f"Proposed: {proposal.get('description', '')}", style="bold magenta")
    ]
    verdict = _VERDICT.get(str(proposal.get("kind")), str(proposal.get("kind", "")))
    parts.append(Text(f"{verdict}", style="magenta"))
    if body := str(proposal.get("body") or "").strip():
        parts.append(Markdown(body, code_theme=palette().code))
    elif proposal.get("kind") == "approve" and not proposal.get("comments"):
        parts.append(Text("LGTM"))  # what an approval with nothing to say posts
    for comment in proposal.get("comments") or []:
        line = Text("• ")
        line.append(f"{comment.get('path')}:{comment.get('line')}", style="cyan")
        line.append(f"  {comment.get('body', '')}")
        parts.append(line)
    for reply in proposal.get("replies") or []:
        line = Text("↳ reply in thread  ", style="dim")
        line.append(str(reply.get("body", "")))
        parts.append(line)
    parts += [_commit(commit) for commit in proposal.get("commits") or []]
    return Group(*parts)


def _commit(commit: dict) -> Text:
    """A commit a push would send: ``• e0b9581  Handle a digit after the degree sign``."""
    line = Text("• ")
    line.append(str(commit.get("sha", ""))[:7], style="cyan")
    line.append(f"  {commit.get('subject', '')}")
    return line


@dataclass(frozen=True)
class _RenderOptions:
    """What the renderers are told besides the entry."""

    full_thinking: bool  # thoughts in full, rather than a line each


def render_entry(entry: dict, *, full_thinking: bool = False) -> RenderableType | None:
    """A transcript entry as Claude Code would show it.

    Subagents' own steps aren't shown, only the call that started each one and
    what it came back with (they're recorded all the same). Each thought is a
    line, unless ``full_thinking``.
    """
    if entry.get("agent"):
        return None
    render = _RENDERERS.get(entry.get("kind"))
    return render(entry, _RenderOptions(full_thinking)) if render else None


def _render_text(entry: dict, options: _RenderOptions) -> RenderableType:
    return _bullet(Markdown(entry["text"], code_theme=palette().code))


def _render_thinking(entry: dict, options: _RenderOptions) -> RenderableType:
    # Like Claude Code: the reasoning, dimmed, ahead of what it leads to. In
    # full it's ten times what the agent says, so a line each unless you ask (t).
    text = entry["text"].strip() if options.full_thinking else thought_headline(entry["text"])
    return _bullet(Text(text, style="dim italic"), style="dim")


def _render_user(entry: dict, options: _RenderOptions) -> RenderableType:
    return _you_wrote(entry["text"])


def _you_wrote(text: str) -> Text:
    """What you wrote, under its ``>``."""
    indented = text.replace("\n", "\n  ")
    return Text(f"> {indented}", style="bold")


def _render_set_aside(entry: dict, options: _RenderOptions) -> RenderableType:
    """A proposal you wrote instead of approving."""
    return Text(f"  ◼ Proposal set aside: {short(entry.get('text', ''))}", style="dim")


# What a tool call is about: the first of these its input has (a description is
# a subagent's task).
_TOOL_TARGETS = ("command", "file_path", "pattern", "path", "url", "description")


def _render_tool_use(entry: dict, options: _RenderOptions) -> RenderableType:
    name, tool_input = entry.get("name", ""), entry.get("input") or {}
    if name == PURR_TOOL_PREFIX + SUBMIT_REVIEW:
        return _bullet(_render_submitted_review(tool_input), style="bold green")
    if name == PURR_TOOL_PREFIX + PROPOSE_ACTION:
        return _bullet(render_proposal(tool_input), style="magenta")
    line = Text(name.removeprefix(PURR_TOOL_PREFIX), style="bold")
    target = next((tool_input[key] for key in _TOOL_TARGETS if tool_input.get(key)), "")
    if target:
        line.append(f"({short(str(target))})", style="dim")
    return _bullet(line, style="green")


def _render_submitted_review(review: dict) -> Group:
    """The review's summary, then its findings, numbered."""
    findings = enumerate(review.get("findings") or [], start=1)
    lines = [Text(review.get("summary", ""), style="bold")]
    lines += [_render_finding(number, finding) for number, finding in findings]
    return Group(*lines)


def _render_finding(number: int, finding: dict) -> Text:
    """``2. issue  src/bus.py:40  The lock is never released.``"""
    severity = finding.get("severity", "suggestion")
    where = finding.get("file") or ""
    if where and finding.get("line"):
        where += f":{finding['line']}"
    line = Text(f"{number}. ")
    line.append(severity, style=_SEVERITY_STYLE.get(severity, ""))
    if where:
        line.append(f"  {where}", style="cyan")
    line.append(f"  {finding.get('text', '')}")
    return line


def _render_tool_result(entry: dict, options: _RenderOptions) -> RenderableType | None:
    text = entry.get("text") or ""
    if text.startswith(("Review recorded", "Proposed (action")):
        return None  # Purr's own tools; their calls already say it all
    if text.startswith(INSTEAD):  # you wrote instead of allowing it
        said = text.removeprefix(INSTEAD).strip()
        return Group(
            Text("  ⎿ Not allowed: you wrote instead", style="dim"), Text(""), _you_wrote(said)
        )
    lines = text.strip().splitlines() or [""]
    more = f" … +{len(lines) - 1} lines" if len(lines) > 1 else ""
    style = "red" if entry.get("is_error") else "dim"
    return Text(f"  ⎿ {short(lines[0], 90)}{more}", style=style)


def _render_result(entry: dict, options: _RenderOptions) -> RenderableType:
    """The end of a turn: ``✓ done · 3 turns · $0.12``."""
    turns, cost = entry.get("turns") or 0, entry.get("cost_usd")
    details = [f"{turns} turn{'s' if turns != 1 else ''}"] if turns else []
    details += [f"${cost:.2f}"] if cost else []
    return Text("  ✓ done" + "".join(f" · {d}" for d in details), style="dim")


def _render_error(entry: dict, options: _RenderOptions) -> RenderableType:
    return Text(f"✗ {entry.get('text', '')}", style="red")


def _render_stopped(entry: dict, options: _RenderOptions) -> RenderableType:
    """You stopped the agent (esc)."""
    return Text("◼ Stopped", style="bold yellow")


def _render_task(entry: dict, options: _RenderOptions) -> RenderableType:
    """A background agent (or command) starting or reporting back."""
    # Stopped isn't failed: the agent stops what it no longer needs.
    mark, verb, style = _TASK_EVENTS.get(entry.get("event"), ("✗", "failed", "dim red"))
    return Text(f"  {mark} background {verb}: {short(entry.get('text', ''), 100)}", style=style)


def _render_permission_auto(entry: dict, options: _RenderOptions) -> RenderableType:
    return Text(f"  ✓ allowed by your permission {entry.get('rule', '')}", style="dim green")


# How each kind of entry shows. A new kind: its renderer, and a line here.
_RENDERERS: dict[str, Callable[[dict, _RenderOptions], RenderableType | None]] = {
    "text": _render_text,
    "thinking": _render_thinking,
    "user": _render_user,
    "set_aside": _render_set_aside,
    "tool_use": _render_tool_use,
    "tool_result": _render_tool_result,
    "result": _render_result,
    "error": _render_error,
    "stopped": _render_stopped,
    "task": _render_task,
    "permission_auto": _render_permission_auto,
}
