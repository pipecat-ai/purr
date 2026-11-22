"""What review agents may do without asking you: standing permissions, per repo or everywhere.

When an agent wants something the review policy doesn't pre-approve (running
tests, editing files, fetching a URL), Purr asks you. "Always" remembers your
answer as a rule, for the PR's repo or for every repo:

- ``Bash(uv run pytest:*)``: that command, with any arguments.
- ``Bash(make test)``: exactly that command.
- ``Edit``, ``Write``, ``WebFetch``, ...: the tool, any use (files only inside
  the PR's worktree).

The rules live in ``permissions.json`` in Purr's config dir, where you can read
and edit them::

    {
      "allow": ["Bash(uv run pytest:*)"],
      "repos": {"pipecat-ai/pipecat": {"allow": ["Bash(make test)"]}}
    }

What keeps a rule from allowing more than you meant:

- It only turns "ask" into "allow": what the policy refuses (GitHub writes,
  pushing, reading your token) stays refused.
- A Bash rule matches one plain command: no chains, pipes, redirections,
  subshells or substitutions, so nothing can ride along. Paths outside the
  PR's worktree still ask.
- File tools only inside the PR's worktree.
- Rules only apply to PRs by the repo's owners, members and collaborators (see
  ``PR.trusted``). An outside contributor's PR always asks: running its tests
  runs its code.
"""

import json
import re
from pathlib import Path

from loguru import logger

from purr.review.policy import (
    _SEPARATORS,
    _SUBSTITUTION,
    _UNSAFE_TOKENS,
    _drop_harmless_redirects,
    _tokens,
)

_RULE = re.compile(r"^(\w+)(?:\((.*)\))?$", re.DOTALL)
# Tools that write files: a rule for them covers the PR's worktree only.
_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Tools that only ask when they'd read outside the worktree: never remembered.
_READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
# A command's leading words that make a rule: programs, subcommands, `-m module`.
_WORD = re.compile(r"[A-Za-z0-9_][\w.+-]*")
_PREFIX_WORDS = 3


def _plain(command: str) -> list[str] | None:
    """The command's words, if it's one plain command; None otherwise."""
    if "\n" in command.strip() or _SUBSTITUTION.search(command):
        return None
    tokens = _tokens(command)
    if tokens is None:
        return None
    tokens = _drop_harmless_redirects(tokens)
    if not tokens or _UNSAFE_TOKENS.intersection(tokens) or _SEPARATORS.intersection(tokens):
        return None
    return tokens


def _inside(path: str, worktree: Path) -> bool:
    root = worktree.resolve()
    resolved = (root / Path(path).expanduser()).resolve()
    return resolved == root or root in resolved.parents


def _looks_like_path(arg: str) -> bool:
    return arg.startswith(("/", "~")) or ".." in arg.split("/")


def rule_for(tool: str, tool_input: dict) -> str | None:
    """The rule "always" would remember for this request; None if none is safe to offer.

    For a command, its leading words (``uv run pytest``), so the same command with
    other arguments is allowed too; the exact command if it starts with a path.
    """
    if tool == "Bash":
        command = str(tool_input.get("command", "")).strip()
        argv = _plain(command)
        if argv is None:
            return None  # a chain or a pipe: allow it once
        words: list[str] = []
        for i, token in enumerate(argv):
            module = token == "-m" and i + 1 < len(argv)
            if len(words) == _PREFIX_WORDS or not (_WORD.fullmatch(token) or module):
                break
            words.append(token)
        if not words:
            return f"Bash({command})"
        return f"Bash({' '.join(words)}:*)"
    if tool in _READ_TOOLS:
        return None
    return tool


def allows(rule: str, tool: str, tool_input: dict, worktree: Path) -> bool:
    """Whether ``rule`` covers this request from an agent working in ``worktree``."""
    match = _RULE.match(rule.strip())
    if not match or match.group(1) != tool:
        return False
    spec = match.group(2)
    if tool == "Bash":
        argv = _plain(str(tool_input.get("command", "")))
        if argv is None or spec is None:
            return False
        if any(_looks_like_path(arg) and not _inside(arg, worktree) for arg in argv[1:]):
            return False
        if spec.endswith(":*"):
            prefix = _tokens(spec[:-2])
            return bool(prefix) and argv[: len(prefix)] == prefix
        return argv == _tokens(spec)
    if spec is not None or tool in _READ_TOOLS:
        return False  # only Bash rules take a pattern; reads outside the worktree always ask
    if tool in _FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return bool(path) and _inside(str(path), worktree)
    return True


class Grants:
    """Your standing permissions, in ``permissions.json`` in Purr's config dir.

    Args:
        root: Purr's config directory.
    """

    def __init__(self, root: Path):
        self.path = root / "permissions.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"Ignoring {self.path}, which can't be read: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def rules(self, repo: str) -> list[str]:
        """The rules that apply in ``repo``: everywhere's, then the repo's own."""
        data = self._load()
        repos = {name.lower(): value for name, value in (data.get("repos") or {}).items()}
        own = (repos.get(repo.lower()) or {}).get("allow") or []
        return [str(rule) for rule in [*(data.get("allow") or []), *own]]

    def match(self, repo: str, tool: str, tool_input: dict, worktree: Path) -> str | None:
        """The first rule that allows this request in ``repo``, if any."""
        return next((r for r in self.rules(repo) if allows(r, tool, tool_input, worktree)), None)

    def add(self, rule: str, repo: str | None = None) -> None:
        """Remember ``rule`` for ``repo``, or everywhere."""
        data = self._load()
        data.setdefault("allow", [])
        data.setdefault("repos", {})
        if repo:
            name = next((n for n in data["repos"] if n.lower() == repo.lower()), repo)
            target = data["repos"].setdefault(name, {}).setdefault("allow", [])
        else:
            target = data["allow"]
        if rule not in target:
            target.append(rule)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".json.tmp")
        staging.write_text(json.dumps(data, indent=2) + "\n")
        staging.replace(self.path)

    def ensure(self) -> Path:
        """The file, created empty if it doesn't exist yet (to edit it)."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"allow": [], "repos": {}}, indent=2) + "\n")
        return self.path
