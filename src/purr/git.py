"""Purr's own clones of the repos it reviews, and a worktree per PR.

Each repo gets one blobless bare clone (``repos/<owner>/<name>.git`` under the
data dir); each PR gets a worktree (``worktrees/<owner>/<name>/<number>``)
detached at the PR's head. File contents download lazily, only for what gets
checked out, and the user's own checkouts are never touched.

A repo's own Claude Code skills, agents and commands come from its default
branch too, packed as a plugin (``plugins/<owner>/<name>/<key>``).
"""

import asyncio
import hashlib
import io
import json
import re
import shutil
import tarfile
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

# What a repo's .claude/ directory can contribute to reviews. Its settings.json
# can't: hooks there run commands.
PLUGIN_PARTS = ("skills", "agents", "commands")
# Frontmatter keys dropped from those files: hooks run commands, and what a
# tool call may do is up to Purr's review policy alone.
_UNSAFE_KEYS = frozenset({"hooks", "allowed-tools", "permissionMode", "mcpServers"})
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?\n)---[ \t]*(?:\n|\Z)", re.DOTALL)
_TOP_LEVEL_KEY = re.compile(r"^([A-Za-z0-9_-]+)[ \t]*:")


def safe_frontmatter(text: str) -> str:
    """``text`` without the frontmatter keys a repo's skills and agents can't set."""
    match = _FRONTMATTER.match(text)
    if not match:
        return text
    kept, dropping = [], False
    for line in match.group(1).splitlines(keepends=True):
        if key := _TOP_LEVEL_KEY.match(line):
            dropping = key.group(1) in _UNSAFE_KEYS
        if not dropping:
            kept.append(line)
    return f"---\n{''.join(kept)}---\n{text[match.end() :]}"


def plugin_name(repo: str) -> str:
    """The plugin's name, so its skills show as ``pipecat:code-review``."""
    return re.sub(r"[^a-z0-9]+", "-", repo.split("/")[-1].lower()).strip("-") or "repo"


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

    async def plugin(self, repo: str) -> Path | None:
        """The repo's skills, agents and commands, from its default branch, as a plugin.

        Like the guidelines, they come from the default branch so a PR can't
        change them. Built once per version of them; None if the repo has none.
        """
        async with self._locks.setdefault(repo, asyncio.Lock()):
            clone = self.clone_path(repo)
            try:
                listing = await self._git(clone, "ls-tree", BASE_REF, ".claude/")
            except GitError:
                return None
            trees = {}  # .claude/skills -> its tree's hash
            for line in listing.splitlines():
                info, _, path = line.partition("\t")
                _, kind, sha = info.split()
                if kind == "tree" and path.removeprefix(".claude/") in PLUGIN_PARTS:
                    trees[path] = sha
            if not trees:
                return None
            key = hashlib.sha1(" ".join(sorted(trees.values())).encode()).hexdigest()[:12]
            versions = self._root / "plugins" / repo
            path = versions / key
            if not path.exists():
                archive = await self._git_bytes(
                    clone, "archive", "--format=tar", BASE_REF, "--", *sorted(trees)
                )
                await asyncio.to_thread(self._unpack_plugin, repo, archive, path)
            for old in versions.iterdir():
                if old != path:
                    shutil.rmtree(old, ignore_errors=True)
            return path

    @staticmethod
    def _unpack_plugin(repo: str, archive: bytes, path: Path) -> None:
        staging = path.with_name(f"{path.name}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(staging, filter="data")
        plugin = staging / ".claude"
        for markdown in plugin.rglob("*.md"):
            if markdown.is_file() and not markdown.is_symlink():
                text = markdown.read_text(errors="replace")
                if (safe := safe_frontmatter(text)) != text:
                    markdown.write_text(safe)
        manifest = {
            "name": plugin_name(repo),
            "description": f"{repo}'s skills and agents, from its default branch (via Purr)",
        }
        (plugin / ".claude-plugin").mkdir()
        (plugin / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))
        plugin.rename(path)
        shutil.rmtree(staging)

    async def _git(self, cwd: Path, *args: str) -> str:
        return (await self._git_bytes(cwd, *args)).decode()

    async def _git_bytes(self, cwd: Path, *args: str) -> bytes:
        return await self._run_bytes("git", "-C", str(cwd), *args)

    @classmethod
    async def _run(cls, *args: str) -> str:
        return (await cls._run_bytes(*args)).decode()

    @staticmethod
    async def _run_bytes(*args: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(f"{' '.join(args[:4])} failed: {stderr.decode().strip()}")
        return stdout
