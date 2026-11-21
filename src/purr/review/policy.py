"""What a review agent may do on its own, what needs you, and what it may never do.

Reviews run on code from anyone who opens a PR, so the defaults are strict:

- **Allowed** without asking: reading and searching inside the PR's worktree
  (and the repo's own skills), loading skills, read-only git and ``gh``
  commands, and a few read-only shell tools.
- **Denied**, always: anything visible on GitHub (reviews, comments, merges,
  ``gh api`` writes, ``git push``) and reading GitHub credentials. The agent
  proposes those as pending actions instead; the app runs them once you approve.
- **Ask** for everything else, including running the PR's code (tests, scripts),
  editing files, fetching URLs, or touching anything outside the worktree.

The policy only looks at what a tool call says it will do. It's a guardrail
against mistakes and prompt injection in PR content, not a sandbox.
"""

import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from purr.review.tools import PURR_TOOL_PREFIX


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""


ALLOW = Verdict(Decision.ALLOW)

GITHUB_WRITE_REASON = (
    "Purr never lets the agent change anything on GitHub. Propose it with the "
    "propose_action tool instead; the user approves or dismisses it."
)

# Tools that only read. Path-taking ones are still checked against the worktree.
_READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
# Skill only loads a skill's instructions; what it then runs is checked as usual.
_SAFE_TOOLS = {"TodoWrite", "WebSearch", "Task", "Agent", "ToolSearch", "Skill"}

# Spotted anywhere in a command, even inside quotes or nested shells.
_GITHUB_WRITES = [
    # git, then only options (and their values), then push:
    # `git -C x push`, but not `git log --grep push`.
    re.compile(r"\bgit\s+(?:-\S+(?:\s+[^-\s]\S*)?\s+)*push\b"),
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
]

# Commands are tokenized quote-aware, so `grep "a|b"` is one command. Chains
# and pipes are fine when every command in them is read-only.
_SEPARATORS = {";", "&&", "||", "|"}
# Writing, backgrounding, subshells and here-docs aren't pre-approved...
_UNSAFE_TOKENS = {">", ">>", "<", "<<", "<<<", ">&", "&>", "&", "(", ")", "|&", ">|"}
# ...and neither is command substitution, even quoted.
_SUBSTITUTION = re.compile(r"`|\$\(")


def _tokens(line: str) -> list[str] | None:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _drop_harmless_redirects(tokens: list[str]) -> list[str]:
    """Remove ``N>/dev/null`` and ``N>&M``, which can't write anything that matters."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        token, following = tokens[i], tokens[i + 1] if i + 1 < len(tokens) else None
        harmless = (token in (">", ">>") and following == "/dev/null") or (
            token == ">&" and following is not None and following.isdigit()
        )
        if harmless:
            if out and out[-1].isdigit():
                out.pop()  # the file descriptor, e.g. the 2 in 2>/dev/null
            i += 2
            continue
        out.append(token)
        i += 1
    return out


_GIT_READ = {
    "diff", "log", "show", "status", "blame", "grep", "ls-files", "ls-tree", "rev-parse",
    "rev-list", "merge-base", "describe", "cat-file", "shortlog", "fetch", "show-ref",
    "name-rev", "range-diff", "diff-tree", "for-each-ref",
}  # fmt: skip
_GH_READ = {
    ("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "list"), ("pr", "status"),
    ("issue", "view"), ("issue", "list"), ("repo", "view"), ("run", "view"), ("run", "list"),
    ("release", "view"), ("release", "list"), ("search", None), ("api", None),
}  # fmt: skip
_READERS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "rg", "tree", "file", "stat", "du",
    "pwd", "echo", "sort", "uniq", "cut", "diff", "cmp", "jq", "nl", "basename", "dirname",
    "realpath", "which", "true",
}  # fmt: skip
_FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}


class Policy:
    """Decides on each tool call a review agent makes in ``worktree``.

    Args:
        worktree: The PR's worktree, where the agent works.
        readable: Other directories it may read without asking (the repo's skills).
    """

    def __init__(self, worktree: Path, readable: Iterable[Path] = ()):
        self._worktree = worktree.resolve()
        self._roots = [self._worktree, *(path.resolve() for path in readable)]

    def check(self, tool: str, tool_input: dict) -> Verdict:
        if tool.startswith(PURR_TOOL_PREFIX) or tool in _SAFE_TOOLS:
            return ALLOW
        if tool in _READ_TOOLS:
            path = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("notebook_path")
            )
            if path and not self._inside(path):
                return Verdict(Decision.ASK, f"reads outside the PR's worktree: {path}")
            return ALLOW
        if tool == "Bash":
            return self.check_command(str(tool_input.get("command", "")))
        return Verdict(Decision.ASK, f"{tool} isn't pre-approved for reviews")

    def check_command(self, command: str) -> Verdict:
        if any(pattern.search(command) for pattern in _GITHUB_WRITES):
            return Verdict(Decision.DENY, GITHUB_WRITE_REASON)
        if _SUBSTITUTION.search(command):
            return Verdict(Decision.ASK, "command substitution isn't pre-approved")
        commands: list[list[str]] = []
        # Lines are separate commands; tokenizing them together would merge them.
        for line in command.splitlines():
            tokens = _tokens(line)
            if tokens is None:
                return Verdict(Decision.ASK, "couldn't parse the command")
            tokens = _drop_harmless_redirects(tokens)
            if _UNSAFE_TOKENS.intersection(tokens):
                return Verdict(Decision.ASK, "redirections and subshells aren't pre-approved")
            current: list[str] = []
            for token in [*tokens, ";"]:
                if token in _SEPARATORS:
                    if current:
                        commands.append(current)
                    current = []
                else:
                    current.append(token)
        if not commands:
            return Verdict(Decision.ASK, "empty command")
        if not all(self._read_only(argv) for argv in commands):
            return Verdict(Decision.ASK, "only read-only commands are pre-approved")
        return ALLOW

    def _read_only(self, argv: list[str]) -> bool:
        program, args = Path(argv[0]).name, argv[1:]
        if any(not self._inside(arg) for arg in args if self._looks_like_path(arg)):
            return False
        if program == "git":
            if args[:1] == ["--no-pager"]:
                args = args[1:]
            return bool(args) and args[0] in _GIT_READ
        if program == "gh":
            if not args:
                return False
            command = (args[0], args[1] if len(args) > 1 else None)
            return command in _GH_READ or (args[0], None) in _GH_READ
        if program == "find":
            return not _FIND_ACTIONS.intersection(args)
        return program in _READERS

    @staticmethod
    def _looks_like_path(arg: str) -> bool:
        return arg.startswith(("/", "~")) or ".." in arg.split("/")

    def _inside(self, path: str) -> bool:
        resolved = (self._worktree / Path(path).expanduser()).resolve()
        return any(resolved == root or root in resolved.parents for root in self._roots)
