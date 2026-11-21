"""Purr's own clones of the repos it reviews, and a worktree per PR.

Each repo gets one blobless bare clone (``repos/<owner>/<name>.git`` under the
data dir); each PR gets a worktree (``worktrees/<owner>/<name>/<number>``)
detached at the PR's head. File contents download lazily, only for what gets
checked out, and the user's own checkouts are never touched.
"""

import asyncio
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from purr.models import PRRef


class GitError(Exception):
    """A git command failed."""


# The repo's default branch, as of the last checkout.
BASE_REF = "refs/purr/base"
GUIDELINE_FILES = ("CLAUDE.md", "AGENTS.md")
_IMPORT = re.compile(r"^@(\S+)\s*$", re.MULTILINE)
_GUIDELINES_LIMIT = 30_000


def gh_clone(repo: str, dest: Path) -> list[str]:
    """Clone with the GitHub CLI, so private repos use the user's GitHub login."""
    return ["gh", "repo", "clone", repo, str(dest), "--", "--bare", "--filter=blob:none"]


class GitRepos:
    """Clones and PR worktrees under ``root``.

    Args:
        root: Purr's data dir.
        clone_command: Builds the command that clones ``repo`` into ``dest``
            (a bare clone with an ``origin`` remote). Tests point it at local repos.
    """

    def __init__(self, root: Path, *, clone_command: Callable[[str, Path], list[str]] = gh_clone):
        self._root = root
        self._clone_command = clone_command
        self._locks: dict[str, asyncio.Lock] = {}

    def clone_path(self, repo: str) -> Path:
        return self._root / "repos" / f"{repo}.git"

    def worktree_path(self, ref: PRRef) -> Path:
        return self._root / "worktrees" / ref.repo / str(ref.number)

    async def checkout(self, ref: PRRef, head_sha: str) -> Path:
        """Make the PR's worktree, detached at ``head_sha``, and return its path."""
        # Git commands on one clone mustn't overlap (index and ref locks).
        async with self._locks.setdefault(ref.repo, asyncio.Lock()):
            clone = self.clone_path(ref.repo)
            if not (clone / "HEAD").exists():
                logger.info(f"Cloning {ref.repo} into {clone}")
                clone.parent.mkdir(parents=True, exist_ok=True)
                await self._run(*self._clone_command(ref.repo, clone))
            # The PR's head, and the default branch (where trusted guidelines live).
            await self._git(
                clone,
                "fetch",
                "--quiet",
                "origin",
                f"+refs/pull/{ref.number}/head:refs/purr/pull/{ref.number}",
                f"+HEAD:{BASE_REF}",
            )
            path = self.worktree_path(ref)
            if (path / ".git").exists():
                await self._git(path, "checkout", "--quiet", "--detach", "--force", head_sha)
            else:
                if path.exists():
                    shutil.rmtree(path)  # a leftover from an interrupted checkout
                await self._git(clone, "worktree", "prune")
                path.parent.mkdir(parents=True, exist_ok=True)
                await self._git(
                    clone, "worktree", "add", "--quiet", "--detach", str(path), head_sha
                )
            return path

    async def guidelines(self, repo: str) -> str:
        """The repo's CLAUDE.md / AGENTS.md from its default branch, with ``@file`` imports.

        Read from the default branch rather than the PR, so a PR can't rewrite
        the rules it's reviewed against. Empty if there are none.
        """
        clone = self.clone_path(repo)
        seen: set[str] = set()
        parts: list[str] = []

        async def read(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            try:
                text = await self._git(clone, "show", f"{BASE_REF}:{name}")
            except GitError:
                return  # no such file
            # `@AGENTS.md` on a line of its own imports that file, as in Claude Code.
            for imported in _IMPORT.findall(text):
                await read(imported.removeprefix("./"))
            text = _IMPORT.sub("", text).strip()
            if text:
                parts.append(f"### {name}\n\n{text}")

        for name in GUIDELINE_FILES:
            await read(name)
        return "\n\n".join(parts)[:_GUIDELINES_LIMIT]

    async def _git(self, cwd: Path, *args: str) -> str:
        return await self._run("git", "-C", str(cwd), *args)

    @staticmethod
    async def _run(*args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(f"{' '.join(args[:4])} failed: {stderr.decode().strip()}")
        return stdout.decode()
