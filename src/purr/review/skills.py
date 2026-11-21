"""A repo's own review skill: finding it among the repo's skills, and using it.

A repo's skills come into reviews as a plugin (see :meth:`purr.git.GitRepos.plugin`).
One of them may be how its maintainers review PRs, like pipecat's
``code-review``. When a repo has one that's clearly for reviewing PRs, Purr's
reviews follow it; when it's less clear (a skill with "review" in its name that
doesn't say it's for PRs, or several of them), the user is asked once. Either
way the choice is kept per repo, and the user can change it.

A review follows a skill by starting with it as a slash command
(``/pipecat:code-review <PR URL>``), the way you'd run it in Claude Code. That
works for skills the model may not invoke on its own too.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.DOTALL)
# Says it reviews PRs: "code review", "PR review", "review a pull request",
# "reviews the current PR" (but not "review concerns" or "Review, refactor...").
_REVIEWS_PRS = re.compile(
    r"\b(?:code|PR|pull[- ]request)[- ]reviews?\b"
    r"|\breview(?:s|ing)?\s+(?:[\w-]+\s+){0,3}?(?:pull[- ]requests?|PRs?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Skill:
    """A skill (or command) that looks like it's for reviewing.

    Parameters:
        name: How it's invoked, plugin included: ``pipecat:code-review``.
        description: What it says it does.
        likely: Clearly for reviewing PRs (its name says review, and its
            description says PRs), so it can be used without asking.
    """

    name: str
    description: str
    likely: bool


def _frontmatter(path: Path) -> dict:
    try:
        match = _FRONTMATTER.match(path.read_text(errors="replace"))
        data = yaml.safe_load(match.group(1)) if match else None
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _plugin_name(plugin: Path) -> str | None:
    try:
        manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    except (OSError, ValueError):
        return None
    name = manifest.get("name") if isinstance(manifest, dict) else None
    return name if isinstance(name, str) and name else None


def review_skills(plugin: Path | None) -> list[Skill]:
    """The plugin's skills and commands that look like they're for reviewing, likeliest first."""
    if plugin is None or not (prefix := _plugin_name(plugin)):
        return []
    found: list[tuple[str, dict]] = []  # (name, frontmatter)
    for path in sorted(plugin.glob("skills/*/SKILL.md")):
        meta = _frontmatter(path)
        found.append((str(meta.get("name") or path.parent.name), meta))
    for path in sorted(plugin.glob("commands/*.md")):
        found.append((path.stem, _frontmatter(path)))
    skills = []
    for name, meta in found:
        if meta.get("user-invocable") is False:
            continue  # it can't be started as a slash command
        description = " ".join(str(meta.get("description") or "").split())
        in_name = "review" in name.lower()
        reviews_prs = bool(_REVIEWS_PRS.search(description))
        if in_name or reviews_prs:
            skills.append(Skill(f"{prefix}:{name}", description, likely=in_name and reviews_prs))
    return sorted(skills, key=lambda skill: not skill.likely)


def skill_prompt(skill: str, url: str) -> str:
    """The prompt that starts a review following ``skill``."""
    return f"/{skill} {url}"


def skill_section(repo: str, skill: str) -> str:
    """How a review that follows a repo's skill fits in Purr, as a system prompt section."""
    return f"""

## This review follows {repo}'s own review skill

The review was started with the repo's `/{skill}` skill. Follow its process, and
its bar for what's worth flagging, rather than the general guidance above. Within
Purr:

- Purr has already decided this PR gets a review: skip the skill's checks on
  whether to review it at all (draft, closed, trivial, already reviewed or
  commented on by Claude).
- Don't post anything to GitHub; Purr's policy refuses `gh pr comment` and
  `gh pr review` anyway. Where the skill says to post comments, put them in
  `propose_action` instead (a summary comment goes in its body). Purr posts them
  once the user approves.
- Finish the way Purr's reviews always do: `submit_review` with the issues the
  skill kept as findings, then `propose_action`.
- On later turns (new commits, the user's questions), keep to the skill's bar."""
