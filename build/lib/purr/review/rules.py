"""Your "always allow" answers, kept as Claude Code permission rules.

When Claude Code asks about something, it suggests the rule that would cover it
(``Bash(uv run pytest:*)``, say). ``A`` at the prompt keeps that rule:

- **everywhere**: in your own Claude Code settings (``~/.claude/settings.json``),
  so it applies in Claude Code itself too;
- **for this repo**: in Purr's Claude Code settings for the repo
  (``~/.config/purr/repos/<owner>/<name>/settings.json``), which reviews there
  load.

Claude Code does all the matching. Both files are ordinary Claude Code
settings, so you can read and edit them like any other (``i``, then ``p``).
"""

import json
from collections.abc import Iterable
from pathlib import Path

from loguru import logger

from purr.review.policy import GITHUB_DENY_RULES

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

# Where a review's commands may connect when Claude Code's sandbox is on.
SANDBOX_DOMAINS = [
    "github.com",
    "api.github.com",
    "*.githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
]


def rule_texts(updates: Iterable) -> list[str]:
    """Claude Code's suggested permission updates as rules: ``Bash(npm test:*)``."""
    texts = []
    for update in updates:
        if getattr(update, "type", None) != "addRules" or getattr(update, "behavior", None) not in (
            None,
            "allow",
        ):
            continue
        for rule in update.rules or []:
            texts.append(
                f"{rule.tool_name}({rule.rule_content})" if rule.rule_content else rule.tool_name
            )
    return texts


class RepoRules:
    """Purr's Claude Code settings per repo: the rules you allowed there, and what every
    review gets (Purr's GitHub rule, the sandbox if it's on).

    Args:
        root: Purr's config directory.
    """

    def __init__(self, root: Path):
        self._root = root

    def path(self, repo: str) -> Path:
        return self._root / "repos" / repo / "settings.json"

    def _load(self, repo: str) -> dict:
        try:
            data = json.loads(self.path(repo).read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.warning(f"Ignoring {self.path(repo)}, which can't be read: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    def rules(self, repo: str) -> list[str]:
        """The rules you allowed in the repo."""
        allowed = (self._load(repo).get("permissions") or {}).get("allow") or []
        return [str(rule) for rule in allowed]

    def add(self, rules: Iterable[str], repo: str) -> None:
        """Allow ``rules`` in the repo from now on."""
        data = self._load(repo)
        allowed = data.setdefault("permissions", {}).setdefault("allow", [])
        allowed.extend(rule for rule in rules if rule not in allowed)
        path = self.path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".json.tmp")
        staging.write_text(json.dumps(data, indent=2) + "\n")
        staging.replace(path)

    def ensure(self, repo: str) -> Path:
        """The repo's file, created empty if it doesn't exist yet (to edit it)."""
        if not self.path(repo).exists():
            self.add([], repo)
        return self.path(repo)

    def settings(self, repo: str, *, sandbox: bool = False) -> str:
        """The Claude Code settings a review in ``repo`` runs with, as JSON."""
        settings: dict = {
            "permissions": {"allow": self.rules(repo), "deny": list(GITHUB_DENY_RULES)}
        }
        if sandbox:
            settings["sandbox"] = {
                "enabled": True,
                "autoAllowBashIfSandboxed": True,
                "network": {"allowedDomains": SANDBOX_DOMAINS},
            }
        return json.dumps(settings)
