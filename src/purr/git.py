"""Purr's own clones of the repos it reviews, and a worktree per PR.

Each repo gets one blobless bare clone (``repos/<owner>/<name>.git`` under the
data dir); each PR gets a worktree (``worktrees/<owner>/<name>/<number>``)
detached at the PR's head. File contents download lazily, only for what gets
checked out, and the user's own checkouts are never touched.

A repo's own Claude Code skills, agents and commands come from its default
branch too, packed as a plugin (``plugins/<owner>/<name>/<key>``).

Open PRs are synced into the clone after each poll (:meth:`GitRepos.sync`), so
their diffs come from here rather than from GitHub: one fetch for the PRs that
are new or moved and the branches they merge into, one for the files their
diffs touch.
"""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from purr.models import PR, PRRef


class GitError(Exception):
    """A git command failed."""


# The repo's default branch, as of the last checkout or sync.
BASE_REF = "refs/purr/base"
PR_REFS = "refs/purr/pull"  # refs/purr/pull/<number>: each open PR's head
BASE_REFS = "refs/purr/heads"  # refs/purr/heads/<branch>: the branches PRs merge into
# Fail fast on a missing object instead of fetching it on its own (blobless clones).
_NO_LAZY_FETCH = {"GIT_NO_LAZY_FETCH": "1"}
# How often bases are refetched when no PR moved (where a PR branched off rarely changes).
BASE_REFRESH = 600
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
        self._bases_fetched: dict[str, float] = {}  # repo -> time.monotonic()

    def clone_path(self, repo: str) -> Path:
        return self._root / "repos" / f"{repo}.git"

    def worktree_path(self, ref: PRRef) -> Path:
        return self._root / "worktrees" / ref.repo / str(ref.number)

    async def checkout(self, ref: PRRef, head_sha: str) -> Path:
        """Make the PR's worktree, detached at ``head_sha``, and return its path."""
        # Git commands on one clone mustn't overlap (index and ref locks).
        async with self._locks.setdefault(ref.repo, asyncio.Lock()):
            clone = await self._clone(ref.repo)
            # The PR's head, and the default branch (where trusted guidelines
            # live): usually here already, from the last sync.
            if not await self._synced(clone, ref, head_sha):
                await self._git(
                    clone,
                    "fetch",
                    "--quiet",
                    "origin",
                    f"+refs/pull/{ref.number}/head:{PR_REFS}/{ref.number}",
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

    async def _synced(self, clone: Path, ref: PRRef, head_sha: str) -> bool:
        """Whether the clone has the PR at ``head_sha`` and a recent default branch."""
        if time.monotonic() - self._bases_fetched.get(ref.repo, -BASE_REFRESH) >= BASE_REFRESH:
            return False
        return (await self._pr_heads(clone)).get(ref.number) == head_sha

    def worktrees(self) -> list[PRRef]:
        """The PRs that have a worktree on disk."""
        found = []
        for path in sorted((self._root / "worktrees").glob("*/*/*")):
            if path.is_dir() and path.name.isdigit():
                found.append(PRRef(f"{path.parent.parent.name}/{path.parent.name}", int(path.name)))
        return found

    async def remove_worktree(self, ref: PRRef) -> bool:
        """Remove the PR's worktree, if it has one; whether there was one.

        Its review session survives (Claude Code keeps it under ~/.claude), and
        :meth:`checkout` makes the worktree again, at the same path, when it's needed.
        """
        path = self.worktree_path(ref)
        if not path.exists():
            return False
        async with self._locks.setdefault(ref.repo, asyncio.Lock()):
            clone = self.clone_path(ref.repo)
            try:
                await self._git(clone, "worktree", "remove", "--force", str(path))
            except GitError:
                shutil.rmtree(path, ignore_errors=True)  # not a worktree git knows
                if (clone / "HEAD").exists():
                    await self._git(clone, "worktree", "prune")
        return True

    async def _has_ref(self, clone: Path, ref: str) -> bool:
        try:
            await self._git(clone, "rev-parse", "--verify", "--quiet", ref)
        except GitError:
            return False
        return True

    async def _clone(self, repo: str) -> Path:
        """The repo's clone, made if needed. Call with the repo's lock held."""
        clone = self.clone_path(repo)
        if not (clone / "HEAD").exists():
            logger.info(f"Cloning {repo} into {clone}")
            clone.parent.mkdir(parents=True, exist_ok=True)
            await self._run(*self._clone_command(repo, clone))
        return clone

    # -- Open PRs, for their diffs ------------------------------------------------------

    @staticmethod
    def _range(pr: PR) -> str:
        """What GitHub diffs: from where the PR branched off its base to its head."""
        base = f"{BASE_REFS}/{pr.base}" if pr.base else BASE_REF
        return f"{base}...{PR_REFS}/{pr.number}"

    async def sync(self, repo: str, prs: list[PR]) -> None:
        """Bring the repo's open PRs (``prs``) into the clone, so their diffs are local.

        Only PRs that are new or have new commits are fetched, and only the
        files their diffs touch. PRs no longer open lose their refs.
        """
        async with self._locks.setdefault(repo, asyncio.Lock()):
            clone = await self._clone(repo)
            have = await self._pr_heads(clone)
            moved = [pr for pr in prs if have.get(pr.number) != pr.head_sha]
            gone = set(have) - {pr.number for pr in prs}
            fresh = time.monotonic() - self._bases_fetched.get(repo, -BASE_REFRESH) < BASE_REFRESH
            if not moved and not gone and fresh:
                return  # nothing new: no network
            bases = sorted({pr.base for pr in prs if pr.base})
            specs = [f"+HEAD:{BASE_REF}"]
            specs += [f"+refs/heads/{base}:{BASE_REFS}/{base}" for base in bases]
            specs += [f"+refs/pull/{pr.number}/head:{PR_REFS}/{pr.number}" for pr in moved]
            await self._fetch(clone, specs)
            self._bases_fetched[repo] = time.monotonic()
            # Makes finding where each PR branched off cheap (ms instead of 50).
            await self._git(clone, "commit-graph", "write", "--reachable", "--no-progress")
            await self._fetch_blobs(clone, moved)
            if gone:
                deletes = "".join(f"delete {PR_REFS}/{number}\n" for number in sorted(gone))
                await self._git(clone, "update-ref", "--stdin", stdin=deletes)

    async def _pr_heads(self, clone: Path) -> dict[int, str]:
        listing = await self._git(
            clone, "for-each-ref", "--format=%(refname:lstrip=3) %(objectname)", PR_REFS
        )
        heads = (line.split() for line in listing.splitlines())
        return {int(number): sha for number, sha in heads if number.isdigit()}

    async def _fetch(self, clone: Path, specs: list[str]) -> None:
        """Fetch refspecs in one go; if that fails (a branch is gone), one by one."""
        try:
            await self._git(
                clone,
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                "--stdin",
                stdin="\n".join(specs) + "\n",
            )
        except GitError as e:
            logger.warning(f"Fetching {len(specs)} refs at once failed ({e}); one by one")
            for spec in specs:
                try:
                    await self._git(clone, "fetch", "--quiet", "--no-tags", "origin", spec)
                except GitError as e:
                    logger.warning(f"Couldn't fetch {spec}: {e}")

    async def _fetch_blobs(self, clone: Path, prs: list[PR]) -> None:
        """Fetch the files these PRs' diffs touch that the clone doesn't have, in one go."""
        wanted: set[str] = set()
        for pr in prs:
            try:  # trees only: no file contents needed to list what changed
                raw = await self._git(
                    clone,
                    "diff",
                    "--raw",
                    "--no-abbrev",
                    "--no-renames",
                    self._range(pr),
                    env=_NO_LAZY_FETCH,
                )
            except GitError as e:
                logger.warning(f"Couldn't list what {pr.ref} changes: {e}")
                continue
            for line in raw.splitlines():
                fields = line.partition("\t")[0].split()
                if len(fields) < 4:
                    continue
                for mode, oid in ((fields[0].lstrip(":"), fields[2]), (fields[1], fields[3])):
                    if mode != "160000" and oid.strip("0"):  # not a submodule, not "none"
                        wanted.add(oid)
        if not wanted:
            return
        check = await self._git(
            clone,
            "cat-file",
            "--batch-check=%(objectname)",
            stdin="\n".join(wanted) + "\n",
            env=_NO_LAZY_FETCH,
        )
        missing = [line.split()[0] for line in check.splitlines() if line.endswith(" missing")]
        if missing:
            await self._git(
                clone,
                "-c",
                "fetch.negotiationAlgorithm=noop",
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                "--recurse-submodules=no",
                "--filter=blob:none",
                "origin",
                "--stdin",
                stdin="\n".join(missing) + "\n",
            )

    async def new_commits(self, pr: PR, since: str) -> tuple[list[str], list[str]] | None:
        """What the PR's commits since ``since`` say and touch, for planning a re-review.

        Returns their subjects (oldest last, as ``git log`` lists them) and the
        files they change (``M src/bus.py``), or None if the clone can't tell.
        The PR's head is fetched if the clone doesn't have it yet; only commits
        and trees are read, so no file contents download.
        """
        clone = self.clone_path(pr.repo)
        if not (clone / "HEAD").exists():
            return None
        async with self._locks.setdefault(pr.repo, asyncio.Lock()):
            try:
                if (await self._pr_heads(clone)).get(pr.number) != pr.head_sha:
                    await self._git(
                        clone,
                        "fetch",
                        "--quiet",
                        "origin",
                        f"+refs/pull/{pr.number}/head:{PR_REFS}/{pr.number}",
                    )
                span = f"{since}..{pr.head_sha}"
                log = await self._git(clone, "log", "--format=%s", span, env=_NO_LAZY_FETCH)
                files = await self._git(
                    clone,
                    "diff",
                    "--name-status",
                    "--no-renames",
                    since,
                    pr.head_sha,
                    env=_NO_LAZY_FETCH,
                )
            except GitError:
                return None
        return log.splitlines(), [" ".join(line.split("\t", 1)) for line in files.splitlines()]

    async def diff(self, pr: PR) -> str | None:
        """The PR's diff from the clone; None if the clone doesn't have it (yet)."""
        clone = self.clone_path(pr.repo)
        if not (clone / "HEAD").exists():
            return None
        try:
            head = await self._git(
                clone, "rev-parse", "--verify", "--quiet", f"{PR_REFS}/{pr.number}"
            )
            if head.strip() != pr.head_sha:
                return None
            return await self._git(
                clone,
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                self._range(pr),
                env=_NO_LAZY_FETCH,
            )
        except GitError:
            return None

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
        The repo is cloned first if it isn't yet.
        """
        async with self._locks.setdefault(repo, asyncio.Lock()):
            clone = await self._clone(repo)
            if not await self._has_ref(clone, BASE_REF):
                await self._git(clone, "fetch", "--quiet", "origin", f"+HEAD:{BASE_REF}")
            listing = await self._git(clone, "ls-tree", BASE_REF, ".claude/")
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

    async def _git(
        self, cwd: Path, *args: str, stdin: str | None = None, env: dict | None = None
    ) -> str:
        output = await self._git_bytes(cwd, *args, stdin=stdin, env=env)
        return output.decode(errors="replace")

    async def _git_bytes(
        self, cwd: Path, *args: str, stdin: str | None = None, env: dict | None = None
    ) -> bytes:
        return await self._run_bytes("git", "-C", str(cwd), *args, stdin=stdin, env=env)

    @classmethod
    async def _run(cls, *args: str) -> str:
        return (await cls._run_bytes(*args)).decode()

    @staticmethod
    async def _run_bytes(*args: str, stdin: str | None = None, env: dict | None = None) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env} if env else None,
        )
        stdout, stderr = await process.communicate(stdin.encode() if stdin is not None else None)
        if process.returncode != 0:
            raise GitError(f"{' '.join(args[:4])} failed: {stderr.decode().strip()}")
        return stdout
