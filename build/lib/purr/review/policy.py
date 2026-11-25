"""The one permission rule Purr adds to Claude Code's: nothing that changes GitHub.

Reviews run with Claude Code's own permissions (see :mod:`purr.review.agent`):
its mode (auto, by default: safe things run, risky ones ask), your Claude Code
settings and rules, and, where it works, its sandbox. Claude Code decides what
runs and what to ask about; when it asks, the question shows in Purr.

On top of that, an agent may never change anything on GitHub (reviews,
comments, merges, ``gh api`` writes, ``git push``) or read your GitHub token:
it proposes, and Purr posts what you approve. That's Claude Code deny rules
(:data:`GITHUB_DENY_RULES`), plus a check before every tool call
(:func:`github_write`) for the forms rules can't see, like
``bash -c 'gh pr merge 12'``.
"""

import re

GITHUB_WRITE_REASON = (
    "Purr never lets the agent change anything on GitHub. Propose it with the "
    "propose_action tool instead; the user approves or dismisses it."
)
# A push gets its own: the agent can't push, but it can propose one.
PUSH_REASON = (
    "Purr never lets the agent push. Propose it instead: commit here, on top of the PR's "
    "head, and call propose_action with kind `push`. Purr shows the user which commits "
    "go to which branch, and pushes them if they approve."
)

# Claude Code permission rules (prefix matches). `gh api` reads are fine, so its
# writes are left to github_write, which sees the method and fields.
GITHUB_DENY_RULES = [
    "Bash(git push:*)",
    "Bash(gh pr review:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh pr merge:*)",
    "Bash(gh pr close:*)",
    "Bash(gh pr edit:*)",
    "Bash(gh pr ready:*)",
    "Bash(gh issue comment:*)",
    "Bash(gh issue close:*)",
    "Bash(gh issue edit:*)",
    "Bash(gh auth token:*)",
    "Read(~/.config/gh/**)",
]

# Spotted anywhere in a command, even inside quotes or nested shells. git, then
# only options (and their values), then push: `git -C x push`, not `git log --grep push`.
_GIT_PUSH = re.compile(r"\bgit\s+(?:-\S+(?:\s+[^-\s]\S*)?\s+)*push\b")
_GITHUB_WRITES = [
    re.compile(
        r"\bgh\s+(?:pr|issue)\s+"
        r"(?:review|comment|merge|close|reopen|edit|create|ready|lock|unlock|delete|transfer|pin|unpin)\b"
    ),
    re.compile(
        r"\bgh\s+(?:release|repo|label|secret|variable|workflow|gist|ruleset)\s+"
        r"(?:create|delete|edit|upload|set|remove|rename|archive|fork|sync|enable|disable|run|clone)\b"
    ),
    re.compile(
        r"\bgh\s+api\b.*(?:-X|--method)[\s=]*['\"]?(?:POST|PUT|PATCH|DELETE)", re.IGNORECASE
    ),
    re.compile(r"\bgh\s+api\b.*\s(?:-f|-F|--field|--raw-field|--input)\b"),
    re.compile(r"\bgh\s+auth\s+(?:token|login|refresh|setup-git)\b"),
    re.compile(r"\.config/gh\b"),  # where gh keeps your token
]


def github_write(tool: str, tool_input: dict) -> str | None:
    """Why the call would change GitHub or read your GitHub token, if it would."""
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        if _GIT_PUSH.search(command):
            return PUSH_REASON
        if any(pattern.search(command) for pattern in _GITHUB_WRITES):
            return GITHUB_WRITE_REASON
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if ".config/gh" in path:
        return "Purr doesn't let the agent read your GitHub credentials."
    return None
