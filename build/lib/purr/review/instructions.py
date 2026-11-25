"""The user's own instructions for reviews: how they like reviews, and how they like code.

Plain Markdown files, like a CLAUDE.md for reviews, each for every repo and
optionally per repo:

- ``review.md``: how reviews should go (tone, what to raise, what to skip).
- ``code.md``: what good code looks like to the user, which reviews hold the
  PR's code to.

All of them are added to the review agent's system prompt. They're sent with
every turn, so edits also apply to sessions already under way.
"""

import re
from enum import StrEnum
from pathlib import Path


class Kind(StrEnum):
    REVIEW = "review"
    CODE = "code"


_TEMPLATES = {
    Kind.REVIEW: """\
<!--
How you like reviews{scope}. Purr adds this to every review agent's instructions.
Write it the way you'd brief a colleague. For example:

- Be terse. Lead with what would block merging.
- Don't comment on formatting; CI takes care of it.
- Ask for tests when behavior changes, not for refactors.

Lines inside these comment markers are ignored.
-->
""",
    Kind.CODE: """\
<!--
How you like code{scope}. Review agents hold the PR's code to this and point out
where it falls short. For example:

- Prefer a generic solution over a pile of special cases.
- Avoid adding flags to keep track of state.
- Keep functions short.

Lines inside these comment markers are ignored.
-->
""",
}

_HEADINGS = {
    Kind.REVIEW: "How the user likes reviews",
    Kind.CODE: "How the user likes code (hold the PR to this, and say where it falls short)",
}

_COMMENTS = re.compile(r"<!--.*?-->", re.DOTALL)


class ReviewInstructions:
    """Where the instruction files live, and what they add up to for a repo.

    Args:
        root: Purr's config directory.
    """

    def __init__(self, root: Path):
        self._root = root

    def path(self, kind: Kind, repo: str | None = None) -> Path:
        """``<root>/<kind>.md`` for all repos; ``<root>/repos/<owner>/<name>/<kind>.md`` for one."""
        if repo:
            return self._root / "repos" / repo / f"{kind}.md"
        return self._root / f"{kind}.md"

    def ensure(self, kind: Kind, repo: str | None = None) -> Path:
        """The file, created with a short how-to if it doesn't exist yet."""
        path = self.path(kind, repo)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            scope = f" in {repo} (on top of your general ones)" if repo else ", in every repo"
            path.write_text(_TEMPLATES[kind].format(scope=scope))
        return path

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return _COMMENTS.sub("", path.read_text()).strip()
        except FileNotFoundError:
            return ""

    def for_repo(self, repo: str) -> str:
        """The instructions that apply to a repo's reviews, as a system prompt section."""
        sections = []
        for kind in Kind:
            if general := self._read(self.path(kind)):
                sections.append(f"## {_HEADINGS[kind]}\n\n{general}")
            if specific := self._read(self.path(kind, repo)):
                sections.append(f"## {_HEADINGS[kind]}, in {repo}\n\n{specific}")
        if not sections:
            return ""
        return (
            "\n\nThe user's own instructions follow. They take precedence over the "
            "defaults above.\n\n" + "\n\n".join(sections)
        )
