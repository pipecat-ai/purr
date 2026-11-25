"""The PR session pane: a Claude Code-style view of a PR's agent session."""

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from purr.jobs import PurrError
from purr.messages import (
    ListRepoSkills,
    MergeReadiness,
    PermissionRequest,
    PlanReviews,
    SendMessage,
    SetModel,
    SuggestPrompt,
)
from purr.models import (
    PR,
    ActionKind,
    CheckState,
    Depth,
    DraftComment,
    GHState,
    Outcome,
    PendingAction,
    PRDetails,
    PRRef,
    PRState,
    Quote,
    Reply,
    Review,
    ReviewPlan,
    ReviewStatus,
)
from purr.review.agent import INSTEAD, thought_headline
from purr.review.skills import RepoSkill
from purr.review.tools import PROPOSE_ACTION, PURR_TOOL_PREFIX, SUBMIT_REVIEW
from purr.tui.commands import (
    PURR_NAMES,
    Command,
    parse,
    render_cost,
    render_help,
    suggestions,
)
from purr.tui.keyhelp import KEYS
from purr.tui.keys import pane_keys
from purr.tui.overview import details_line, render_overview
from purr.tui.prompt import HistorySearchScreen, PromptArea
from purr.tui.theme import palette, theme_names
from purr.tui.transcript import Transcript
from purr.tui.working import Working
from purr.workers.reviews import STOPPED

if TYPE_CHECKING:
    from purr.tui.app import PurrApp

_SEVERITY_STYLE = {"issue": "bold red", "suggestion": "yellow", "nit": "dim"}


def number_style() -> str:
    """PR numbers' style, wherever they show."""
    return f"bold {palette().number}"


# A background task's event: its mark, what it says, its style.
_TASK_EVENTS = {
    "started": ("◷", "started", "dim"),
    "completed": ("✓", "done", "dim green"),
    "stopped": ("◼", "stopped", "dim"),
    "killed": ("◼", "stopped", "dim"),
}
# Absolute paths into a PR's worktree read better relative to it.
_WORKTREE_PREFIX = re.compile(r"\S*/worktrees/[^/\s]+/[^/\s]+/\d+/")


# What a does to a proposal, by its kind, as the footer says it: so a never surprises.
A_DOES = {
    ActionKind.COMMENT: "Post comment",
    ActionKind.APPROVE: "Approve PR",
    ActionKind.REQUEST_CHANGES: "Request changes",
    ActionKind.PUSH: "Push",
    ActionKind.MERGE: "Merge",
}


def needs_you_for(
    pr: PR, ask: PermissionRequest | None, proposal: ActionKind | None, *, drafted: bool,
    failed: bool,
) -> str:  # fmt: skip
    """What a PR in Needs you waits on you for, in a few words, the most pressing first (as
    the decision box asks): the agent's question, its proposal (what a does), your
    comments, replies, a failed review; else, your verdict."""
    if ask:
        return f"Asks: {_short(ask.description, 80)}"
    if proposal:
        return f"{A_DOES[proposal]}?"
    if drafted:
        return "Post your comments?"
    if pr.replies:
        return replied(pr.replies)
    return "Review failed" if failed else "No verdict yet"


@dataclass
class SessionState:
    """What the UI keeps about a PR's session between redraws."""

    entries: list[dict] | None = None  # None until the transcript is loaded
    progress: str | None = None  # what the review's doing; None once nothing is
    waiting: bool = False  # the review waits on you (a question)
    since: float | None = None  # when the agent started working (monotonic)
    background: int = 0  # its tasks (agents, commands) still running in the background
    ask: PermissionRequest | None = None
    details_error: str | None = None  # why the description couldn't be loaded
    approved: str | None = None  # the commit you approved it at, through Purr
    merge: MergeReadiness | None = None  # where it stood for merging when you pressed m
    mine: list[DraftComment] = field(default_factory=list)  # your comments, not posted yet
    proposal: ActionKind | None = None  # what the agent proposes (the oldest), as last read
    verdict_due: bool = False  # it waits for your verdict alone: a approves it
    quote: Quote | None = None  # the lines of the diff you're asking about, if any
    suggestion: str = ""  # what you might ask next (purr.suggest)
    suggested_at: int | None = None  # how far the transcript was when it was asked for

    def suggested(self) -> str:
        """The suggestion, if it's still for where the conversation is."""
        return self.suggestion if self.suggested_at == len(self.entries or []) else ""

    def a_does(self) -> str:
        """What a does now, as its key says it ("" for nothing): allow what the agent asks,
        do what it proposes, or post your comments."""
        if self.ask:
            return "Allow once"
        if self.proposal:
            return A_DOES[self.proposal]
        if self.mine:
            return "Post comments"
        return "Approve PR" if self.verdict_due else ""

    @property
    def working(self) -> bool:
        """The agent's at it (not waiting on you, or for its turn): the spinners spin."""
        return bool(self.progress) and self.since is not None and not self.waiting and not self.ask


def _bullet(body: RenderableType, style: str = "") -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(ratio=1)
    grid.add_row(Text("⏺", style=style or "white"), body)
    return grid


def _short(value: str, width: int = 100) -> str:
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


def replied(replies: Sequence[Reply]) -> str:
    """'alice replied', 'alice and bob replied (3)'."""
    authors = list(dict.fromkeys(reply.author for reply in replies))
    who = authors[0] if len(authors) == 1 else f"{', '.join(authors[:-1])} and {authors[-1]}"
    return f"{who} replied" + (f" ({len(replies)})" if len(replies) > 1 else "")


def render_replies(replies: Sequence[Reply], shown: int = 3) -> Text:
    """What others said to you since your last word, the latest few, and what x does."""
    text = Text(f"{replied(replies)}:", style="bold")
    for reply in replies[-shown:]:
        where = f" · {reply.where}" if reply.where else ""
        body = " ".join(reply.body.split())
        text.append(f"\n  {reply.author}{where}: ", style="cyan")
        text.append(_short(body, 110))
    if len(replies) > shown:
        text.append(f"\n  and {len(replies) - shown} before", style="dim")
    text.append("\n[x] no reply needed", style="bold")
    text.append("   or answer: here (ask the agent) or on GitHub", style="dim")
    return text


def outcome_mark(outcome: Outcome) -> Text:
    """What a review found, at a glance: ✓ nothing (nits don't count), ● issues."""
    return Text("●", style="bold red") if outcome.found else Text("✓", style="bold green")


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
    return Text(f"  ◼ Proposal set aside: {_short(entry.get('text', ''))}", style="dim")


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
        line.append(f"({_short(str(target))})", style="dim")
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
    return Text(f"  ⎿ {_short(lines[0], 90)}{more}", style=style)


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
    return Text(f"  {mark} background {verb}: {_short(entry.get('text', ''), 100)}", style=style)


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


def _review_status(
    pr: PR,
    review: Review | None,
    outcome: Outcome,
    actions: list[PendingAction],
    state: SessionState,
) -> Text:
    """The header's line on the review: waiting on you, at work, how it went, or not done."""
    if state.ask:
        return Text("⚠ Waiting for you to allow something", style="bold yellow")
    if pr.state == PRState.REVIEWING:
        progress = f" · {_short(state.progress, 80)}" if state.progress else ""
        return Text.assemble(("⟳ Reviewing", "bold cyan"), (progress, "dim"))
    if state.progress:  # the lead is planning its review
        return Text(f"⟳ {_short(state.progress, 80)}", style="bold cyan")
    if review is None:
        return _not_reviewed(pr)
    if review.status == ReviewStatus.FAILED and review.error == STOPPED:
        return Text.assemble(("◼ Review stopped", "bold yellow"), (" · r to review it", "dim"))
    if review.status == ReviewStatus.FAILED:
        return Text(f"✗ Review failed: {review.error}", style="bold red")
    if review.status == ReviewStatus.FINISHED:
        mark = outcome_mark(outcome)
        kind = _review_kind(review.plan)
        pending = f" · {len(actions)} pending action" if actions else ""
        said = f" {kind}: {outcome.text()}"
        return Text.assemble(mark, (said, mark.style), (pending, "yellow"))
    return Text()


def _review_kind(plan: ReviewPlan | None) -> str:
    """What the review was: how deep it went (a quick look deserves less trust than a full
    review), or a reading of the comments on your PR."""
    if plan is None:
        return "Review"
    return "Comments" if plan.answers_comments else f"{plan.depth.value.capitalize()} review"


def _not_reviewed(pr: PR) -> Text:
    """Nobody's review yet, or only others': and how to start yours. Yours, it says so."""
    if pr.authored:
        return Text.assemble(
            ("◆ Your PR", "bold blue"), (" · r to review it yourself · d diff", "dim")
        )
    text = Text()
    text.append("◌ Not reviewed by you" if pr.others_on_it else "○ Not reviewed", style="bold")
    if pr.requested_from_me:
        text.append(" · asks for your review", style="magenta")
    if pr.others_on_it:
        hint = " · r to review it too"
    else:
        hint = " · r to review it" + ("" if pr.assigned_to_me else " and take it")
    text.append(f"{hint} · d diff", style="dim")
    return text


def _worktree_note(worktree_path: str) -> str | None:
    """Where the review's worktree is (~/... for your home); None once it's removed."""
    path = Path(worktree_path)
    if not path.exists():
        return None
    home = str(Path.home())
    return "Worktree " + ("~" + str(path)[len(home) :] if str(path).startswith(home) else str(path))


def _about(quote: Quote) -> str:
    """How a question about lines of the diff starts: ``About llm_service.py:404–412:``."""
    return f"About {quote.ref()}:"


def _plan_note(plan: ReviewPlan) -> str:
    """What the lead planned, and why: while the review waits or runs (its place in line,
    how deep it goes)."""
    if plan.answers_comments:
        return "Reading what others said on your PR, to propose what to change and answer"
    line = f"Planned: a {plan.depth.value} review, {plan.priority.value} priority"
    return line + (f". {plan.why}" if plan.why else "")


def _decision(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """What waits on you, above the prompt: the first of the questions that has something."""
    asked = (question(pr, state, actions) for question in _QUESTIONS)
    return next((text for text in asked if text is not None), None)


def _permission_question(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """The agent wants to do something Claude Code asks about first."""
    if not (ask := state.ask):
        return None
    always = "   [A] always…" if ask.rule else ""
    return Text.assemble(
        ("The agent wants to: ", "bold"),
        (_short(ask.description, 120), "yellow"),
        ("\n", ""),
        (ask.reason or "Claude Code asks before this.", "dim"),
        (f"\n[a] allow once{always}   [x] deny", "bold"),
        ("   or write what to do instead", "dim"),
    )


def _proposal_question(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """The agent proposes posting something (the oldest proposal first)."""
    if not actions:
        return None
    action = actions[0]
    if action.kind == ActionKind.PUSH:
        return _push_question(action)
    comments = len(action.payload.get("comments") or [])
    plural = "s" if comments != 1 else ""
    detail = f" · {comments} inline comment{plural}" if comments else ""
    return Text.assemble(
        (f"{action.description}?", "bold"),
        (detail, "dim"),
        (f" · and {_yours(state.mine)}" if state.mine else "", "yellow"),
        (f"\n[a] {_lower_first(A_DOES[action.kind])}   [x] dismiss", "bold"),
        ("   or write to change it", "dim"),
    )


def _lower_first(label: str) -> str:
    """``approve PR``, from ``Approve PR``."""
    return label[:1].lower() + label[1:]


def _push_question(action: PendingAction, shown: int = 3) -> Text:
    """The agent proposes pushing the commits it made: which, the latest few."""
    commits = action.payload.get("commits") or []
    text = Text(f"{action.description}?", style="bold")
    for commit in commits[:shown]:
        text.append(f"\n  {str(commit.get('sha', ''))[:7]} ", style="cyan")
        text.append(_short(str(commit.get("subject", "")), 110))
    if len(commits) > shown:
        text.append(f"\n  and {len(commits) - shown} before", style="dim")
    text.append("\n[a] push   [x] dismiss", style="bold")
    text.append("   only if nobody pushed to it since", style="dim")
    return text


def _yours(mine: list[DraftComment]) -> str:
    return f"your {len(mine)} comment{'s' if len(mine) != 1 else ''}"


def _comments_question(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """You wrote comments on the diff, and there's no proposal for them to go with."""
    if not state.mine or actions:
        return None
    return Text.assemble(
        (f"Post {_yours(state.mine)}?", "bold"),
        ("\n[a] post them", "bold"),
        ("   as a review comment · d shows them in the diff", "dim"),
    )


def _replies_question(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """Others replied to you on GitHub: answer, or say no reply's needed."""
    return render_replies(pr.replies) if pr.replies else None


def _verdict_question(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """Reviewed, and nothing's proposed or said to you: it waits for your verdict alone."""
    if not state.verdict_due or state.mine:
        return None
    return Text.assemble(
        ("No verdict yet.", "bold"),
        ("\n[a] approve PR", "bold"),
        ("   or ask the agent to request changes", "dim"),
    )


def _verdict_due(pr: PR, review: Review | None, actions: list[PendingAction]) -> bool:
    """Someone else's PR in Needs you, reviewed, with nothing proposed or said to you: it
    waits for your verdict alone."""
    reviewed = review is not None and review.status == ReviewStatus.FINISHED
    alone = not actions and not pr.replies
    return pr.state == PRState.NEEDS_YOU and not pr.authored and reviewed and alone


def _merge_question(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """You pressed m: where it stands for merging, for your final decision."""
    return render_merge(pr, state.merge) if state.merge else None


def _merge_hint(pr: PR, state: SessionState, actions: list[PendingAction]) -> Text | None:
    """You approved it as it is, and nothing else waits on you: it can be merged."""
    if pr.gh_state != GHState.OPEN or not state.approved or state.approved != pr.head_sha:
        return None
    return Text.assemble(
        ("You approved it.", "bold"),
        ("\n[m] merge…", "bold"),
        ("   shows its checks and reviews first", "dim"),
    )


# What the decision box can ask, most pressing first; each says None when it has
# nothing to ask. A new question: its function, and a line here.
_QUESTIONS: list[Callable[[PR, SessionState, list[PendingAction]], Text | None]] = [
    _permission_question,
    _merge_question,
    _proposal_question,
    _comments_question,
    _replies_question,
    _verdict_question,
    _merge_hint,
]

# GitHub's names for its merge buttons, by merge method.
_MERGE_VERBS = {"merge": "Merge", "squash": "Squash and merge", "rebase": "Rebase and merge"}


def render_merge(pr: PR, readiness: MergeReadiness) -> Text:
    """Where the PR stands for merging: what blocks it, or what you'd be merging past."""
    if readiness.blockers:
        text = Text(f"Can't merge #{pr.number} yet:", style="bold")
        for blocker in readiness.blockers:
            text.append(f"\n  ✗ {blocker}", style="red")
        text.append("\n[m] check again", style="bold")
        return text
    check = readiness.check
    verb = _MERGE_VERBS.get(check.method, "Merge")
    text = Text(f"{verb} #{pr.number} into {check.base}?", style="bold")
    text.append(f"\n  ✓ you approved it at {check.head_sha[:7]}", style="green")
    if passed := sum(run.state == CheckState.PASSED for run in check.checks):
        text.append(f"\n  ✓ {passed} check{'s' if passed != 1 else ''} passed", style="green")
    for concern in readiness.concerns:
        text.append(f"\n  ⚠ {concern}", style="yellow")
    text.append("\n[m] merge anyway" if readiness.concerns else "\n[m] merge", style="bold")
    text.append("   or move on to leave it", style="dim")
    return text


def _suggestion(command: Command) -> Text:
    """A / suggestion: ``/review [quick|full]  Start over…``."""
    return Text.assemble(
        (f"/{command.name}", "bold"), (f"  {command.args}", ""), (f"  {command.help}", "dim")
    )


class PRSession(Vertical):
    """Header, transcript, the decision box, and the prompt for one PR.

    The transcript opens with the PR's overview (description and files), so a
    PR nobody has reviewed yet shows what it's about, and a reviewed one keeps
    it above the session.
    """

    BINDINGS = [
        KEYS,
        Binding("enter", "ask", "Ask"),
        # The transcript, wherever the focus is in the pane, typing included.
        Binding("pageup", "page(-1)", "Page up", show=False, priority=True),
        Binding("pagedown", "page(1)", "Page down", show=False, priority=True),
        # As in Emacs, reading; typing, ctrl+v pastes (what you copied from the pane).
        Binding("ctrl+v", "page(1)", "Page down", show=False),
        Binding("alt+v", "page(-1)", "Page up", show=False),
        Binding("ctrl+r", "search_history", "Search history"),
        # While the agent's at work on the PR, esc stops it, as in Claude Code (zoomed, it
        # zooms out first: the repo's). In the prompt: the / suggestions close, or back to
        # the pane. Elsewhere it's the repo's (zoom out, home).
        Binding("escape", "repo('stop')", "Stop"),
        Binding("escape", "leave_prompt", "Back"),
        # The repo's keys, for reading and deciding (they work from the list too).
        *pane_keys(
            ["review", "approve", "dismiss", "merge", "move(1)", "move(-1)", "diff", "thinking"]
            + ["open_claude", "shell", "zoom", "switch_pane"],
            switch_pane="Switch",  # to the list, or the diff in its place
        ),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pr: PR | None = None

    @property
    def purr_app(self) -> "PurrApp":
        return cast("PurrApp", self.app)

    def compose(self) -> ComposeResult:
        yield Static(id="pr-header")
        # min_width: RichLog draws at least 78 columns wide, cutting off a narrower pane's.
        yield Transcript(id="transcript", wrap=True, markup=False, auto_scroll=True, min_width=20)
        yield Working(id="working")
        yield Static(id="pr-decision")
        suggestions = OptionList(id="suggestions")
        suggestions.can_focus = False
        yield suggestions
        yield PromptArea(id="prompt")

    def on_mount(self) -> None:
        self.border_title = "PR"
        self.query_one("#pr-decision").display = False
        self.query_one("#suggestions").display = False
        self.query_one(PromptArea).completer = self
        self._suggested: list[Command] = []
        self._drawn: PRRef | None = None  # the PR whose transcript is on screen
        self._a_does: str | None = None  # what the footer said a does, last time
        self._wrote = False  # anything in the transcript yet (for the blank lines)

    # -- Slash commands (see purr.tui.commands) ----------------------------------------

    def _skills(self) -> list[RepoSkill]:
        """The repo's skills, for suggestions; fetched in the background the first time."""
        if not self.pr:
            return []
        repo = self.pr.repo
        cache = self.purr_app.repo_skills
        if repo not in cache:
            cache[repo] = []
            self.run_worker(self._load_skills(repo), group="skills")
        return cache[repo]

    async def _load_skills(self, repo: str) -> list[RepoSkill]:
        try:
            reply = await self.purr_app.bus.request(ListRepoSkills(repo=repo))
            self.purr_app.repo_skills[repo] = reply.skills
        except PurrError:
            self.purr_app.repo_skills.pop(repo, None)  # try again next time
            return []
        self._suggest()
        return self.purr_app.repo_skills[repo]

    @on(TextArea.Changed, "#prompt")
    def _suggest(self) -> None:
        box = self.query_one("#suggestions", OptionList)
        self._suggested = suggestions(self.query_one(PromptArea).text, self._skills())
        box.display = bool(self._suggested)
        if self._suggested:
            box.set_options([Option(_suggestion(command)) for command in self._suggested])
            box.highlighted = 0

    def active(self) -> bool:
        return bool(self._suggested) and self.query_one("#suggestions").display

    def close_suggestions(self) -> bool:
        """Close the / suggestions, if they're open (esc)."""
        if not self.active():
            return False
        self.query_one("#suggestions").display = False
        self._suggested = []
        return True

    def move(self, step: int) -> None:
        box = self.query_one("#suggestions", OptionList)
        box.action_cursor_down() if step > 0 else box.action_cursor_up()

    def accept(self) -> str | None:
        index = self.query_one("#suggestions", OptionList).highlighted
        if index is None or index >= len(self._suggested):
            return None
        return f"/{self._suggested[index].name}"

    async def _command(self, name: str, args: str) -> None:
        """Run one of Purr's commands; its output goes in the transcript, just for you."""
        assert self.pr
        self.query_one(RichLog).write(Text(f"\n> /{name} {args}".rstrip(), style="bold"))
        if run := self._COMMAND_HANDLERS.get(name):
            await run(self, self.pr.ref, args)

    async def _help(self, ref: PRRef, args: str) -> None:
        """/help: what you can type here, the repo's skills included."""
        self.query_one(RichLog).write(render_help(await self._load_skills(ref.repo)))

    async def _review_again(self, ref: PRRef, args: str) -> None:
        """/review and /clear: start over with a fresh review, quick or full if you say."""
        log = self.query_one(RichLog)
        if args not in ("", "quick", "full"):
            log.write(Text("Say /review, /review quick or /review full.", style="yellow"))
            return
        depth = Depth(args) if args else None
        self.purr_app.main.repo.zoom_in_on_pr()  # to follow it, as r does
        errors = (await self.purr_app.bus.request(PlanReviews(prs=[ref], depth=depth))).errors
        for error in errors.values():
            log.write(Text(error, style="yellow"))
        if not errors:
            log.write(Text("Starting over with a fresh review.", style="dim"))

    async def _model(self, ref: PRRef, args: str) -> None:
        """/model: choose the model for this PR's conversation, then say which it uses."""
        assert self.pr
        if args:
            model = None if args in ("default", "auto") else args
            await self.purr_app.bus.request(SetModel(pr=ref, model=model))
            self.pr = await self.purr_app.purr.store.get_pr(ref) or self.pr
        chosen = self.pr.model
        config = self.purr_app.purr.config
        text = (
            f"This PR's conversation uses {chosen}."
            if chosen
            else f"Purr's choice: {config.review_model or 'Claude Code’s default'}, and "
            f"{config.quick_model or 'the same'} for quick reviews. /model <name> to choose."
        )
        self.query_one(RichLog).write(Text(text, style="dim"))

    async def _cost(self, ref: PRRef, args: str) -> None:
        """/cost: what the PR's runs cost, and how long they took."""
        self.query_one(RichLog).write(render_cost(await self.purr_app.purr.store.runs(ref)))

    async def _shell(self, ref: PRRef, args: str) -> None:
        await self.purr_app.main.repo.action_shell()

    async def _claude(self, ref: PRRef, args: str) -> None:
        await self.purr_app.main.repo.action_open_claude()

    async def _theme(self, ref: PRRef, args: str) -> None:
        """/theme: the picker; /theme nord: that one, now and from now on."""
        if not args:
            self.purr_app.choose_theme()
            return
        if args not in (names := theme_names()):
            said = f"No theme {args!r}. Themes: {', '.join(names)}"
            self.query_one(RichLog).write(Text(said, style="yellow"))
            return
        await self.purr_app.keep_theme(args)

    # What runs each of Purr's commands (listed, with their help, in purr.tui.commands).
    # A new command: its method here, and a line in this table.
    _COMMAND_HANDLERS = {
        "help": _help,
        "review": _review_again,
        "clear": _review_again,
        "model": _model,
        "cost": _cost,
        "shell": _shell,
        "claude": _claude,
        "theme": _theme,
    }

    def state(self, pr: PR | None = None) -> SessionState:
        pr = pr or self.pr
        assert pr is not None
        return self.purr_app.sessions.setdefault(str(pr.ref), SessionState())

    async def show(self, pr: PR) -> None:
        """Show a PR (or refresh the one shown).

        Redraws when the transcript shows anything else: going by what was last
        drawn rather than what was last asked for, a show cut short (moving on
        quickly, switching repos) can't leave another PR's transcript behind.
        """
        self.pr = pr
        await self.refresh_status()
        state = self.state(pr)
        loaded = state.entries is None
        if loaded:
            state.entries = await self._load_history(pr)
        if not self._showing(pr):
            return  # moved on meanwhile (another PR, another repo)
        if loaded or self._drawn != pr.ref:
            self.redraw()
            asked = await self.purr_app.purr.store.prompts(pr.ref)
            if self._showing(pr):
                self.query_one(PromptArea).set_history([text for _, text in reversed(asked)])

    def _showing(self, pr: PR) -> bool:
        """Whether it's this PR in the pane still: the store's answers take a moment, and
        moving on meanwhile (another PR, another repo) shows another, or none. Quitting
        meanwhile takes the pane's widgets away."""
        return self.app.is_running and self.pr is not None and self.pr.ref == pr.ref

    def clear(self) -> None:
        """Show nothing, until the next PR is shown (another repo's is on its way)."""
        self.pr, self._drawn = None, None
        self.query_one("#pr-header", Static).update("")
        self.query_one(RichLog).clear()
        self.query_one("#pr-decision").display = False
        self.query_one(Working).stop()
        self.query_one(PromptArea).suggest("")

    @on(Transcript.Rewrap)
    def _rewrap(self) -> None:
        """Wider or narrower (z, or the terminal resized): written again, to fill it."""
        if self.pr is not None:
            self.redraw(keep_place=True)

    def redraw(self, *, keep_place: bool = False) -> None:
        """Rewrite the transcript: the overview, then the session.

        ``keep_place``: stay about as far down it as you were, rather than at its end.
        """
        assert self.pr is not None
        state = self.state()
        log = self.query_one(RichLog)
        place = log.scroll_y / log.max_scroll_y if log.max_scroll_y else 1.0
        log.clear()
        overview = render_overview(self.purr_app.details.get(self.pr), state.details_error)
        log.write(overview, scroll_end=False)
        self._wrote = True
        for entry in state.entries or []:
            self._write(entry)
        if not state.entries:
            log.scroll_home(animate=False)
        elif keep_place and place < 1:
            # After the writes' own scrolling to the end.
            log.call_after_refresh(
                lambda: log.scroll_to(y=round(place * log.max_scroll_y), animate=False)
            )
        self._drawn = self.pr.ref

    async def details_changed(self) -> None:
        """The shown PR's details arrived (or failed to)."""
        await self.refresh_status()
        self.redraw()

    async def _load_history(self, pr: PR) -> list[dict]:
        review = await self.purr_app.purr.store.latest_review(pr.ref)
        if not review or not review.session_id or not review.worktree_path:
            return []
        agent = self.purr_app.purr.agent
        try:
            return await asyncio.to_thread(
                agent.history, review.session_id, Path(review.worktree_path)
            )
        except Exception:
            return []

    def append(self, pr_key: str, entry: dict) -> None:
        """A live transcript entry for some PR; drawn if it's the one shown."""
        state = self.purr_app.sessions.setdefault(pr_key, SessionState())
        if state.entries is None:
            return  # not loaded yet; it'll come with the history
        state.entries.append(entry)
        if self._drawn and str(self._drawn) == pr_key:
            self._write(entry)

    def _write(self, entry: dict) -> None:
        """One transcript entry, a blank line apart from the one before it.

        Like Claude Code, a tool call and its result are one step: no line between.
        """
        app = self.purr_app
        if (renderable := render_entry(entry, full_thinking=app.full_thinking)) is None:
            return
        log = self.query_one(RichLog)
        if self._wrote and entry.get("kind") != "tool_result":
            log.write(Text(""))
        log.write(renderable)
        self._wrote = True

    async def refresh_status(self) -> None:
        if not (shown := self.pr):
            return
        store = self.purr_app.purr.store
        pr = await store.get_pr(shown.ref) or shown
        review = await store.latest_review(pr.ref)
        outcome = Outcome.of(await store.findings(review.id)) if review else Outcome()
        actions = await store.pending_actions(pr.ref)
        approved = await store.approved_head(pr.ref)
        mine = await store.draft_comments(pr.ref)
        up_next = next((u.why for u in await store.up_next(pr.repo) if u.pr == pr.ref), "")
        if not self._showing(pr):
            return
        self.pr = pr
        state = self.state()
        state.approved, state.mine = approved, mine
        state.proposal = actions[0].kind if actions else None
        state.verdict_due = _verdict_due(pr, review, actions)
        if state.a_does() != self._a_does:  # the footer says what a does now
            self._a_does = state.a_does()
            self.refresh_bindings()
        details = self.purr_app.details.get(pr)
        self.query_one("#pr-header", Static).update(
            self._header(pr, review, outcome, actions, state, details, up_next)
        )
        self._show_working(state)
        self._suggest_next()
        box = self.query_one("#pr-decision", Static)
        if (decision := _decision(pr, state, actions)) is not None:
            box.update(decision)
        box.display = decision is not None

    def _show_working(self, state: SessionState) -> None:
        working = self.query_one(Working)
        if not state.working:
            working.stop()
            return
        assert state.since is not None
        working.start(state.since, state.background)

    @staticmethod
    def _header(
        pr: PR,
        review: Review | None,
        outcome: Outcome,
        actions: list[PendingAction],
        state: SessionState,
        details: PRDetails | None,
        up_next: str = "",
    ) -> Text:
        text = Text()
        text.append(f"#{pr.number}", style=number_style())
        text.append(f"  {pr.title}\n", style="bold")
        text.append(details_line(pr, details))
        text.append("\n")
        text.append(_review_status(pr, review, outcome, actions, state))
        if up_next and review is None:  # why the lead thinks it's worth looking at next
            text.append(f"\n➜ Up next: {up_next}", style="green")
        if pr.replies and review is not None:
            text.append(f" · {replied(pr.replies)}", style="yellow")
        if review and review.worktree_path and (where := _worktree_note(review.worktree_path)):
            text.append(f"\n{where}", style="dim")
        if review and review.plan and pr.state == PRState.REVIEWING:
            text.append(f"\n{_plan_note(review.plan)}", style="dim")
        return text

    def focus_prompt(self) -> None:
        self.query_one(PromptArea).focus()

    def focus_transcript(self) -> None:
        self.query_one("#transcript").focus()

    # -- What you might ask next (see purr.suggest) -------------------------------------

    def on_descendant_focus(self) -> None:
        self._suggest_next()

    def _suggest_next(self) -> None:
        """Show the suggestion for where the PR's conversation is, asking for one if
        there's none yet: only once the agent's done, and for the PR you're in."""
        if not self.pr:
            return
        state = self.state()
        self.query_one(PromptArea).suggest(state.suggested())
        if not self._wants_suggestion(state):
            return
        state.suggestion, state.suggested_at = "", len(state.entries or [])
        self.run_worker(self._fetch_suggestion(self.pr.ref, state), group="suggestion")

    def _wants_suggestion(self, state: SessionState) -> bool:
        asked = state.suggested_at == len(state.entries or [])
        idle = not state.progress and not state.ask
        return bool(state.entries) and idle and not asked and self.has_focus_within

    async def _fetch_suggestion(self, ref: PRRef, state: SessionState) -> None:
        at = state.suggested_at
        try:
            suggestion = await self.purr_app.bus.request(SuggestPrompt(pr=ref))
        except PurrError:
            return
        if state.suggested_at != at:
            return  # the conversation moved on meanwhile
        state.suggestion = suggestion.text
        if self.pr and self.pr.ref == ref:
            self.query_one(PromptArea).suggest(state.suggested())

    def ask_about(self, quote: Quote) -> None:
        """Ask the agent about lines of the diff: the prompt starts with which, and they go to
        it with what you write (as long as that starts so)."""
        self.state().quote = quote
        prompt = self.query_one(PromptArea)
        prompt.set_text(f"{_about(quote)} ")
        prompt.focus()

    def action_leave_prompt(self) -> None:
        """esc in the prompt: the / suggestions close first, then back to the pane."""
        if not self.close_suggestions():
            self.focus_transcript()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Typing, enter sends and esc leaves; reading, enter goes to the prompt. esc stops
        the agent at work first (unless it closes the / suggestions, or zooms out)."""
        if parameters == ("stop",):
            repo = self.purr_app.main.repo
            suggesting = self.query_one("#suggestions").display
            return bool(repo.at_work()) and not suggesting and not repo.zoomed
        if parameters == ("approve",):  # only when there's something for it to do
            return bool(self.purr_app.main.repo.a_does())
        typing = self.query_one("#prompt").has_focus
        if action in ("search_history", "leave_prompt"):
            return typing
        return not (action == "ask" and typing)

    async def action_repo(self, action: str) -> None:
        await self.purr_app.main.repo.run_action(action)

    def action_ask(self) -> None:
        self.focus_prompt()

    def action_page(self, step: int) -> None:
        log = self.query_one(RichLog)
        log.scroll_page_down() if step > 0 else log.scroll_page_up()

    async def action_search_history(self) -> None:
        prompt = self.query_one(PromptArea)
        asked = await self.purr_app.purr.store.prompts()

        def picked(text: str | None) -> None:
            if text is not None:
                prompt.set_text(text)
            prompt.focus()

        self.app.push_screen(HistorySearchScreen(asked, prompt.text), picked)

    def _quoted(self, text: str) -> Quote | None:
        """The lines you asked about, if what you wrote still starts with them."""
        quote = self.state().quote
        return quote if quote and text.startswith(_about(quote)) else None

    @on(PromptArea.Submitted)
    async def send(self, event: PromptArea.Submitted) -> None:
        text = event.text.strip()
        if not text or not self.pr:
            return
        command = parse(text)
        if command and command[0] in PURR_NAMES:
            event.prompt.clear()
            event.prompt.remember(text)
            try:
                await self._command(*command)
            except PurrError as e:
                self.notify(str(e), severity="warning")
            return
        quote = self._quoted(text)
        try:
            request = SendMessage(pr=self.pr.ref, text=text, quote=quote)
            sent = await self.purr_app.bus.request(request)
        except PurrError as e:
            self.notify(str(e), severity="warning")
            return
        self.state().quote = None
        event.prompt.clear()
        event.prompt.remember(text)
        ref = str(self.pr.ref)
        for proposal in sent.set_aside:
            self.append(ref, {"kind": "set_aside", "text": proposal})
        if not sent.answered:  # if it did, it shows under the call it said no to
            self.append(ref, {"kind": "user", "text": text})
