"""Review sessions on the Claude Agent SDK (Claude Code as a library).

A review is one Agent SDK session per PR, working in the PR's worktree. The
agent reports through two in-process tools, ``submit_review`` and
``propose_action``; it can't touch GitHub (see :mod:`purr.review.policy`). The
session ID is chosen up front, so a review can be resumed after a restart,
from the TUI, or in Claude Code itself (``claude --resume``).
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    get_session_info,
    get_session_messages,
    tool,
)
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from purr.claude import log_stderr
from purr.models import PR, ActionKind, Finding, ReviewPlan, Severity
from purr.review.instructions import ReviewInstructions
from purr.review.plan import plan_section
from purr.review.policy import GITHUB_DENY_RULES, github_write
from purr.review.rules import RepoRules, rule_texts
from purr.review.skills import skill_prompt, skill_section
from purr.review.tools import (
    GET_REVIEW_THREADS,
    PROPOSE_ACTION,
    PROPOSE_ACTION_SCHEMA,
    PURR_SERVER,
    PURR_TOOL_PREFIX,
    SUBMIT_REVIEW,
    SUBMIT_REVIEW_SCHEMA,
)

INSTRUCTIONS = """\
You are reviewing a GitHub pull request for the user, inside Purr, a PR review app.
The PR's head is checked out in the current directory (a detached git worktree).

How to review:
- Start with the PR's description and diff: `gh pr view {number} --repo {repo}` and
  `gh pr diff {number} --repo {repo}`. If the PR links issues ("Fixes #123", or
  linked issues in the description), read them with `gh issue view` to know what
  the change is meant to do.
- Call `get_review_threads` to see the review conversation so far, so you don't
  repeat what's already been said.
- Read the code around the change until you understand it. Look for bugs,
  regressions, missing or weak tests, security problems and confusing code. Be
  concrete: say what's wrong, where, and why it matters. Skip praise, and skip
  style nits a linter would catch unless they really matter.
- Read-only commands run without asking. Anything else (running the PR's code,
  editing files, fetching URLs) waits for the user to allow it, which may take
  a while, so only ask when it really helps. Don't modify files. If something
  you needed was refused, say what you couldn't verify in your findings.

You can't post to GitHub or merge, and must not try. When you're done:
1. Call `submit_review` once, with a short summary and your findings.
2. Call `propose_action` once with what you recommend: `request_changes` (with
   the inline comments to post), `approve`, or `comment`. When a point belongs
   in an existing thread, put it in `replies` (with the thread's id) instead of
   a new comment. The user approves or dismisses it, and Purr posts it for them.
Then stop.

Afterwards the user keeps talking with you in this conversation: questions,
digging deeper, and deciding what to post. When they ask to change what gets
posted (drop a comment, reword one, change the verdict), call `propose_action`
again with the whole updated proposal; it replaces the previous one. Same if
your recommendation changes.
"""


def set_aside_note(proposals: list[str]) -> str:
    """For the agent, ahead of your message: you wrote instead of approving its proposal."""
    what = "; ".join(f"“{p}”" for p in proposals)
    return (
        f"<purr-note>The user wrote instead of approving your proposal ({what}), so it was "
        "set aside. Answer them; then, if you still recommend posting something, call "
        "propose_action again, changed or as it was.</purr-note>\n\n"
    )


_NOTE = re.compile(r"\A\s*<purr-note>.*?</purr-note>\s*", re.DOTALL)


def set_aside_in(message: str) -> list[str]:
    """The proposals a message of yours set aside, from its note to the agent."""
    note = _NOTE.match(message)
    return re.findall(r"“(.+?)”", note.group(0)) if note else []


RESUME_PROMPT = (
    "Purr was restarted while you were reviewing. Continue where you left off, "
    "and finish by calling submit_review and propose_action."
)


def guidelines_section(repo: str, guidelines: str) -> str:
    """The repo's own guidelines (from its default branch) as a system prompt section."""
    if not guidelines:
        return ""
    return (
        f"\n\n## {repo}'s own guidelines\n\nFrom the repo's CLAUDE.md / AGENTS.md on its "
        "default branch. Hold the PR to them where they apply, and say which rule a "
        f"finding comes from.\n\n{guidelines}"
    )


def rereview_prompt(old_sha: str | None, new_sha: str, skill: str | None = None) -> str:
    """The follow-up that re-reviews a PR after new commits, in the same session."""
    since = f"`git diff {old_sha} {new_sha}`" if old_sha else "the PR's latest commits"
    process = f", following the {skill} skill's process again," if skill else ""
    return (
        f"The author pushed new commits; the PR's head is now {new_sha}, checked out here. "
        f"Review what changed since your last review{process} ({since}; the earlier commits "
        "are still available locally). Say which of your earlier findings are resolved and "
        "which still stand, and look for new problems in what changed. Check the review "
        "threads first (get_review_threads): reply in existing threads rather than "
        "repeating yourself, and on threads about your earlier findings, say whether "
        "they're resolved. Then call submit_review with the updated findings and "
        "propose_action with what to post now."
    )


def review_prompt(pr: PR, skill: str | None = None) -> str:
    """What starts a review: the repo's own review skill, if it has one Purr uses."""
    if skill:
        return skill_prompt(skill, pr.url)
    return f'Review {pr.repo}#{pr.number}: "{pr.title}" by {pr.author} ({pr.url}).'


@dataclass
class ReviewCallbacks:
    """How a running review reports back to the review worker.

    Parameters:
        on_message: Each transcript entry, as a plain dict (see :func:`transcript_entries`).
        on_submit: The agent's summary and findings.
        on_propose: A proposed GitHub action ``(kind, description, payload)``;
            returns the pending action's ID.
        on_ask: Claude Code wants to ask about a tool call ``(tool, input,
            reason)``; returns whether the user allows it, or None if nobody
            answered in time.
        on_threads: The PR's review threads, for ``get_review_threads``.
    """

    on_message: Callable[[dict], Awaitable[None]]
    on_submit: Callable[[str, list[Finding]], Awaitable[None]]
    on_propose: Callable[[ActionKind, str, dict], Awaitable[str]]
    on_ask: Callable[[str, dict, str, list[str]], Awaitable["PermissionAnswer"]]
    on_threads: Callable[[], Awaitable[list[dict]]]


@dataclass
class PermissionAnswer:
    """Your answer when Claude Code asks about a tool call.

    Parameters:
        allowed: Yes or no; None if nobody answered in time.
        remember: Keep Claude Code's suggested rule: ``repo`` (this repo's
            reviews) or ``all`` (your Claude Code settings: everywhere).
        instead: What you wrote instead of answering: a no, and what to do
            (Claude Code's "No, and tell Claude what to do differently").
    """

    allowed: bool | None
    remember: str | None = None
    instead: str | None = None


# How a no with what to do instead starts, for the agent; the transcript shows
# what follows as your message.
INSTEAD = "The user didn't allow this, and wrote instead:"


def permission_result(
    answer: PermissionAnswer, suggestions: list, repo: str, rules: RepoRules | None
) -> PermissionResultAllow | PermissionResultDeny:
    """What Claude Code gets back for an answer, with the rule to keep, if any."""
    if answer.allowed is None:
        return PermissionResultDeny(
            message="The user didn't answer; they may be away. Carry on without it, and at "
            "the end say what you wanted to run and why, so they can allow it later."
        )
    if not answer.allowed and answer.instead:
        return PermissionResultDeny(message=f"{INSTEAD}\n\n{answer.instead}")
    if not answer.allowed:
        return PermissionResultDeny(
            message="The user said no. Carry on without it, and say in your findings what "
            "you couldn't check."
        )
    updates = []
    if answer.remember == "all":  # your Claude Code settings: everywhere
        updates = [replace(update, destination="userSettings") for update in suggestions]
    elif answer.remember == "repo":
        if rules:
            rules.add(rule_texts(suggestions), repo)  # this repo's next reviews
        updates = [replace(update, destination="session") for update in suggestions]
    return PermissionResultAllow(updated_permissions=updates or None)


@dataclass
class AgentResult:
    text: str | None = None
    is_error: bool = False
    error: str | None = None
    cost_usd: float | None = None
    turns: int = 0
    denials: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    api_duration_ms: int | None = None
    model: str | None = None
    usage: dict = field(default_factory=dict)  # tokens, as the API reports them


class ReviewAgent(Protocol):
    def session_exists(self, session_id: str, worktree: Path) -> bool: ...

    def history(self, session_id: str, worktree: Path) -> list[dict]:
        """The session's transcript so far, as entries (blocking: reads a file)."""
        ...

    async def run(
        self,
        *,
        pr: PR,
        worktree: Path,
        session_id: str,
        resume: bool,
        prompt: str,
        callbacks: ReviewCallbacks,
        guidelines: str = "",
        plugin: Path | None = None,
        skill: str | None = None,
        plan: ReviewPlan | None = None,
        model: str | None = None,
    ) -> AgentResult: ...


def _text(content: Any, limit: int = 2000) -> str:
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item) for item in content
        )
    else:
        text = "" if content is None else str(content)
    return text if len(text) <= limit else text[:limit] + "…"


def transcript_entries(message: Any) -> list[dict]:
    """An SDK message as plain, JSON-friendly transcript entries for the bus and UI.

    Entries from a subagent (the Agent tool) carry ``agent``: the ID of the tool
    call that started it.
    """
    entries = _entries(message)
    if agent := getattr(message, "parent_tool_use_id", None):
        for entry in entries:
            entry["agent"] = agent
    return entries


def track_tasks(message: Any, running: dict[str, str]) -> None:
    """Keep ``running`` (task ID -> what it is) up to date with the session's background tasks."""
    if isinstance(message, TaskStartedMessage):
        running[message.task_id] = message.description
    elif isinstance(message, TaskNotificationMessage):
        running.pop(message.task_id, None)
    elif isinstance(message, TaskUpdatedMessage):
        status = (getattr(message, "patch", None) or {}).get("status")
        if status in TERMINAL_TASK_STATUSES:
            running.pop(message.task_id, None)


def _entries(message: Any) -> list[dict]:
    if isinstance(message, TaskStartedMessage):
        return [
            {"kind": "task", "event": "started", "text": message.description, "id": message.task_id}
        ]
    if isinstance(message, TaskNotificationMessage):
        return [
            {
                "kind": "task",
                "event": message.status,
                "text": message.summary,
                "id": message.task_id,
            }
        ]
    if isinstance(message, AssistantMessage):
        entries = []
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                entries.append({"kind": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                entries.append({"kind": "thinking", "text": block.thinking})
            elif isinstance(block, ToolUseBlock):
                entries.append(
                    {"kind": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )
        return entries
    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            return [{"kind": "user", "text": message.content}]
        return [
            {
                "kind": "tool_result",
                "tool_use_id": block.tool_use_id,
                "is_error": bool(block.is_error),
                "text": _text(block.content),
            }
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
    if isinstance(message, ResultMessage):
        return [
            {
                "kind": "result",
                "is_error": message.is_error,
                "text": message.result,
                "turns": message.num_turns,
                "cost_usd": message.total_cost_usd,
                "duration_ms": message.duration_ms,
                "api_duration_ms": message.duration_api_ms,
                "model": _models(message),
                "usage": message.usage or {},
            }
        ]
    return []


def _models(message: ResultMessage) -> str | None:
    return ", ".join(message.model_usage) if message.model_usage else None


def history_entries(session_messages: list[Any]) -> list[dict]:
    """The SDK's stored transcript (raw API messages) as transcript entries."""
    entries: list[dict] = []
    for item in session_messages:
        body = item.message if isinstance(item.message, dict) else {}
        content = body.get("content")
        if item.type == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    entries.append({"kind": "text", "text": block["text"]})
                elif block.get("type") == "thinking" and str(block.get("thinking") or "").strip():
                    entries.append({"kind": "thinking", "text": block["thinking"]})
                elif block.get("type") == "tool_use":
                    entries.append(
                        {
                            "kind": "tool_use",
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input") or {},
                        }
                    )
        elif item.type == "user":
            if isinstance(content, str):
                entries += [{"kind": "set_aside", "text": p} for p in set_aside_in(content)]
                entries.append({"kind": "user", "text": _as_typed(content)})
            elif isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        entries.append(
                            {
                                "kind": "tool_result",
                                "tool_use_id": block.get("tool_use_id"),
                                "is_error": bool(block.get("is_error")),
                                "text": _text(block.get("content")),
                            }
                        )
                    elif block.get("type") == "text":
                        entries.append({"kind": "user", "text": block.get("text", "")})
    return entries


_COMMAND = re.compile(
    r"\A\s*<command-message>.*?</command-message>\s*<command-name>(.*?)</command-name>"
    r"\s*(?:<command-args>(.*?)</command-args>)?\s*\Z",
    re.DOTALL,
)


def _as_typed(text: str) -> str:
    """A message as it was typed, rather than as the session stores it: a slash command
    as typed, and without Purr's notes to the agent."""
    text = _NOTE.sub("", text)
    if match := _COMMAND.match(text):
        return " ".join(part.strip() for part in match.groups() if part and part.strip())
    return text


_SENTENCE = re.compile(r"(.+?[.?!])(?:\s|$)")


def thought_headline(text: str) -> str:
    """A thought in a line, as Claude Code shows it: its heading or first sentence.

    ``Checking the lock file. The bump is deliberate, since ...`` becomes
    ``Checking the lock file…``.
    """
    text = text.strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first = first.strip("*#_ ")  # a **heading**
    if match := _SENTENCE.match(first):
        first = match.group(1)
    if first != text:
        first = first.rstrip(".:;, ") + "…"
    return first


def describe_tool_use(name: str, tool_input: dict) -> str:
    """A one-line description of a tool call, for progress lines."""
    if name == PURR_TOOL_PREFIX + SUBMIT_REVIEW:
        return "Writing up the review"
    if name == PURR_TOOL_PREFIX + PROPOSE_ACTION:
        return f"Proposing: {tool_input.get('description', '')}"
    if name == "Bash":
        return f"$ {tool_input.get('command', '')}"
    target = (
        tool_input.get("file_path")
        or tool_input.get("pattern")
        or tool_input.get("path")
        or tool_input.get("description")  # a subagent's task
    )
    return f"{name} {target}" if target else name


def _findings(raw: list[dict]) -> list[Finding]:
    findings = []
    for item in raw:
        try:
            severity = Severity(item.get("severity", "suggestion"))
        except ValueError:
            severity = Severity.SUGGESTION
        findings.append(
            Finding(
                severity=severity,
                text=str(item.get("text", "")),
                file=item.get("file"),
                line=item.get("line") if isinstance(item.get("line"), int) else None,
            )
        )
    return findings


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


class ClaudeReviewAgent:
    """Runs reviews as Claude Agent SDK sessions.

    Permissions are Claude Code's own (see :mod:`purr.review.policy`): its
    ``permission_mode``, your Claude Code settings, the rules you allowed for
    the repo, and its sandbox if ``sandbox``. Purr only adds: nothing that
    changes GitHub.

    Args:
        model: Model to use; None uses Claude Code's configured default.
        max_budget_usd: Optional spending cap per run.
        instructions: Your review and code style.
        rules: The rules you allowed per repo ("always" at a permission prompt).
        permission_mode: Claude Code's permission mode for reviews.
        sandbox: Run the agents' commands in Claude Code's sandbox.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_budget_usd: float | None = None,
        instructions: ReviewInstructions | None = None,
        rules: RepoRules | None = None,
        permission_mode: str = "auto",
        sandbox: bool = False,
    ):
        self._model = model
        self._max_budget_usd = max_budget_usd
        self._instructions = instructions
        self._rules = rules
        self._permission_mode = permission_mode
        self._sandbox = sandbox

    def session_exists(self, session_id: str, worktree: Path) -> bool:
        return get_session_info(session_id, directory=str(worktree)) is not None

    def history(self, session_id: str, worktree: Path) -> list[dict]:
        return history_entries(get_session_messages(session_id, directory=str(worktree)))

    async def run(
        self,
        *,
        pr: PR,
        worktree: Path,
        session_id: str,
        resume: bool,
        prompt: str,
        callbacks: ReviewCallbacks,
        guidelines: str = "",
        plugin: Path | None = None,
        skill: str | None = None,
        plan: ReviewPlan | None = None,
        model: str | None = None,
    ) -> AgentResult:

        @tool(
            SUBMIT_REVIEW,
            "Submit your review: a short summary and your findings.",
            SUBMIT_REVIEW_SCHEMA,
        )
        async def submit_review(args: dict) -> dict:
            await callbacks.on_submit(
                str(args.get("summary", "")), _findings(args.get("findings", []))
            )
            return _ok("Review recorded. Now call propose_action.")

        @tool(
            PROPOSE_ACTION,
            "Propose a GitHub review for the user to approve; Purr posts it if they do.",
            PROPOSE_ACTION_SCHEMA,
        )
        async def propose_action(args: dict) -> dict:
            try:
                kind = ActionKind(args.get("kind"))
            except ValueError:
                return {**_ok(f"Unknown kind {args.get('kind')!r}."), "is_error": True}
            payload = {
                "body": args.get("body", ""),
                "comments": args.get("comments", []),
                "replies": args.get("replies", []),
            }
            action_id = await callbacks.on_propose(kind, str(args.get("description", "")), payload)
            return _ok(f"Proposed (action {action_id}). The user will approve or dismiss it.")

        @tool(
            GET_REVIEW_THREADS,
            "The PR's review threads: id, file and line, resolved or not, and the comments "
            "(by_user marks the user's own).",
            {"type": "object", "properties": {}},
        )
        async def get_review_threads(args: dict) -> dict:
            return _ok(json.dumps(await callbacks.on_threads(), indent=1))

        async def pre_tool_use(hook_input: Any, tool_use_id: str | None, context: Any) -> dict:
            # Purr's one rule. Everything else is up to Claude Code's permissions.
            reason = github_write(hook_input["tool_name"], hook_input.get("tool_input") or {})
            if not reason:
                return {}
            decision = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
            return {"hookSpecificOutput": decision}

        async def can_use_tool(name: str, tool_input: dict, context: Any) -> Any:
            # Claude Code wants to ask: the question goes to Purr's UI.
            suggestions = list(getattr(context, "suggestions", None) or [])
            reason = getattr(context, "decision_reason", None) or getattr(context, "title", None)
            answer = await callbacks.on_ask(name, tool_input, reason or "", rule_texts(suggestions))
            return permission_result(answer, suggestions, pr.repo, self._rules)

        options = ClaudeAgentOptions(
            cwd=str(worktree),
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": INSTRUCTIONS.format(repo=pr.repo, number=pr.number)
                + guidelines_section(pr.repo, guidelines)
                + (skill_section(pr.repo, skill) if skill else "")
                + (plan_section(plan) if plan else "")
                + (self._instructions.for_repo(pr.repo) if self._instructions else ""),
            },
            mcp_servers={
                PURR_SERVER: create_sdk_mcp_server(
                    PURR_SERVER, tools=[submit_review, propose_action, get_review_threads]
                )
            },
            # The repo's own skills and agents, from its default branch.
            plugins=[{"type": "local", "path": str(plugin)}] if plugin else [],
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            can_use_tool=can_use_tool,
            permission_mode=self._permission_mode,  # type: ignore[arg-type]
            # The rules you allowed for the repo, Purr's GitHub rule, the sandbox.
            settings=(
                self._rules.settings(pr.repo, sandbox=self._sandbox)
                if self._rules
                else json.dumps({"permissions": {"deny": GITHUB_DENY_RULES}})
            ),
            disallowed_tools=list(GITHUB_DENY_RULES),
            # Only the user's own settings: a PR's .claude/settings.json could define
            # hooks, and those run commands. The repo's skills come in as a plugin.
            setting_sources=["user"],
            # Summaries of the agent's reasoning, shown in the PR session like
            # Claude Code does (the default, "omitted", returns them empty).
            thinking={"type": "adaptive", "display": "summarized"},
            model=model or self._model,
            stderr=log_stderr,
            max_budget_usd=self._max_budget_usd,
            resume=session_id if resume else None,
            session_id=None if resume else session_id,
        )

        result = AgentResult()
        running: dict[str, str] = {}  # background tasks (agents, shells) still at work
        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for message in client.receive_messages():
                entries = transcript_entries(message)
                for entry in entries:
                    if entry["kind"] == "task" and entry.get("id") in running:
                        # Say which task it was: its report is often a note to the agent
                        # ("delivered to you as a message from ...").
                        entry["text"] = running[entry["id"]]
                out = len(running)
                track_tasks(message, running)
                if len(running) != out:  # for the working line; not part of the transcript
                    entries.append({"kind": "background", "running": len(running)})
                for entry in entries:
                    if entry["kind"] == "result" and running:
                        continue  # a turn's end, not the run's: agents are still at work
                    await callbacks.on_message(entry)
                if isinstance(message, ResultMessage):
                    # Figures are running totals: the last result has the whole run's.
                    result = AgentResult(
                        text=message.result,
                        is_error=message.is_error,
                        error=message.result if message.is_error else None,
                        cost_usd=message.total_cost_usd,
                        turns=message.num_turns,
                        denials=[
                            json.dumps(d, default=str) for d in (message.permission_denials or [])
                        ],
                        duration_ms=message.duration_ms,
                        api_duration_ms=message.duration_api_ms,
                        model=_models(message),
                        usage=message.usage or {},
                    )
                    # A turn can end with agents still working in the background
                    # ("I'll get back to you"): Claude Code carries on when they
                    # report, so the run isn't over until none are left.
                    if not running:
                        break
        return result


# Opening a session in Claude Code itself (the `o` key). Same protections as in
# Purr: only the user's settings load (a PR's could define hooks), and GitHub
# writes are disallowed. Everything else gets Claude Code's usual prompts.
CLAUDE_CODE_NOTE = (
    "You're in Purr's worktree for {pr}, continuing a review session. Don't post to "
    "GitHub or push; Purr's submit_review and propose_action tools aren't available "
    "here, so describe any recommendation and the user will act on it in Purr."
)
# In Claude Code itself Purr's check doesn't run, so `gh api` goes entirely.
CLAUDE_CODE_DISALLOWED = [*GITHUB_DENY_RULES, "Bash(gh api:*)"]


def claude_code_command(session_id: str, pr: str, claude: str = "claude") -> list[str]:
    """The command that resumes a review session in Claude Code.

    ``--resume`` keeps the session ID (``--fork-session`` would make a new one),
    so Purr picks the same session up again afterwards.
    """
    return [
        claude,
        "--resume",
        session_id,
        "--setting-sources",
        "user",
        "--append-system-prompt",
        CLAUDE_CODE_NOTE.format(pr=pr),
        "--disallowedTools",
        *CLAUDE_CODE_DISALLOWED,
    ]
