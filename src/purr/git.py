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
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from purr.diff import unquoted
from purr.models import PR, FileChange, PRRef


class GitError(Exception):
    """A git command failed."""


@dataclass(frozen=True)
class Local:
    """What the agent committed in a PR's worktree that isn't on GitHub yet.

    Attributes:
        head: What's checked out there.
        commits: How many commits it made on top of the PR's head (a merge is one); 0 if
            it rewrote the PR instead (a rebase, say).
    """

    head: str
    commits: int


@dataclass(frozen=True)
class Unpushed:
    """What pushing a PR's worktree would send to its branch.

    Attributes:
        commits: ``(sha, subject)``, newest first: the commits made on top of the PR's
            head or, rewritten (a rebase, say), all the branch's commits as they'd be.
        replaces: Rewritten, how many of the branch's commits they replace; 0 on top.
    """

    commits: list[tuple[str, str]]
    replaces: int = 0


# The repo's default branch, as of the last checkout or sync.
BASE_REF = "refs/purr/base"
PR_REFS = "refs/purr/pull"  # refs/purr/pull/<number>: each open PR's head
BASE_REFS = "refs/purr/heads"  # refs/purr/heads/<branch>: the branches PRs merge into
_BASE_REFSPEC = f"+HEAD:{BASE_REF}"
# In each worktree, its own: the PR's head Purr last put it on (or found it built on).
_PUT_ON = "refs/worktree/purr/head"
# Fail fast on a missing object instead of fetching it on its own (blobless clones).
_NO_LAZY_FETCH = {"GIT_NO_LAZY_FETCH": "1"}
# A diff as GitHub (and gh pr diff) shows it.
_DIFF = ("diff", "--no-color", "--no-ext-diff", "--src-prefix=a/", "--dst-prefix=b/")
# What git leaves in a worktree's git dir while something's halfway, and what aborts it
# (git am keeps its state where a rebase does, with an "applying" file: it comes first).
_UNFINISHED = (
    ("rebase-apply/applying", ("am", "--abort")),
    ("rebase-merge", ("rebase", "--abort")),
    ("rebase-apply", ("rebase", "--abort")),
    ("MERGE_HEAD", ("merge", "--abort")),
    ("CHERRY_PICK_HEAD", ("cherry-pick", "--abort")),
    ("REVERT_HEAD", ("revert", "--abort")),
)
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


def push_url(origin: str, base: str, head: str) -> str:
    """Where to push to ``head`` (owner/name: a PR's fork, often) from the clone of ``base``
    whose remote is ``origin``: the same host and protocol (https or ssh), ``head``'s repo."""
    if head.lower() == base.lower():
        return origin
    repo = re.compile(rf"(?<=[/:]){re.escape(base)}(?=(?:\.git)?/?$)", re.IGNORECASE)
    url, found = repo.subn(head, origin)
    if not found:
        raise GitError(f"Can't tell how to reach {head} from {origin}")
    return url


def _pr_refspec(number: int) -> str:
    """Fetches a PR's head into the clone's ref for it."""
    return f"+refs/pull/{number}/head:{PR_REFS}/{number}"


def _base_ref(branch: str) -> str:
    """The clone's ref for a branch PRs merge into ("": the default one)."""
    return f"{BASE_REFS}/{branch}" if branch else BASE_REF


def _base_refspec(branch: str) -> str:
    """Fetches a branch PRs merge into ("": the default one) into the clone's ref for it."""
    return f"+refs/heads/{branch}:{_base_ref(branch)}" if branch else _BASE_REFSPEC


def _command(args: tuple[str, ...]) -> str:
    """A command, as an error names it: ``git stash push``, git's own options (``-C dir``,
    ``-c key=value``) left out, which would hide what it was."""
    program, *rest = args
    while rest[:1] in (["-C"], ["-c"]):
        rest = rest[2:]
    return " ".join([program, *rest[:2]])


def _blobs(raw_diff: str) -> set[str]:
    """The files (blob ids) on both sides of each change in a ``git diff --raw``."""
    sides = []
    for line in raw_diff.splitlines():
        # :<old mode> <new mode> <old blob> <new blob> <status>\t<path>
        fields = line.partition("\t")[0].split()
        if len(fields) >= 4:
            sides += [(fields[0].lstrip(":"), fields[2]), (fields[1], fields[3])]
    # Not a submodule, and not "none" (all zeros: the file was added or deleted).
    return {oid for mode, oid in sides if mode != "160000" and oid.strip("0")}


def _file_change(numstat: str) -> FileChange:
    """A line of ``git diff --numstat``: added, removed, path (a binary file's counts: -)."""
    added, removed, path = numstat.split("\t", 2)
    return FileChange(
        unquoted(path),
        int(added) if added.isdigit() else 0,
        int(removed) if removed.isdigit() else 0,
    )


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

    def _lock(self, repo: str) -> asyncio.Lock:
        """The repo's lock: git commands on one clone mustn't overlap (index and ref locks)."""
        return self._locks.setdefault(repo, asyncio.Lock())

    async def checkout(self, ref: PRRef, head_sha: str) -> Path:
        """Make the PR's worktree, detached at ``head_sha``, and return its path."""
        async with self._lock(ref.repo):
            clone = await self._clone(ref.repo)
            # The PR's head, and the default branch (where trusted guidelines
            # live): usually here already, from the last sync.
            if not await self._synced(clone, ref, head_sha):
                await self._git(
                    clone, "fetch", "--quiet", "origin", _pr_refspec(ref.number), _BASE_REFSPEC
                )
            path = self.worktree_path(ref)
            if (path / ".git").exists():
                await self._move_worktree(path, ref, head_sha)
            else:
                await self._add_worktree(clone, path, head_sha)
            return path

    async def _move_worktree(self, path: Path, ref: PRRef, head_sha: str) -> None:
        """Move the PR's existing worktree to ``head_sha``.

        The PR's head is where Purr put the worktree last, or what's checked out is it
        or on top of it: leave the worktree as it is, with whatever you (or the agent)
        did there: changes, commits, a rebase not pushed yet, a rebase or a merge
        stopped on a conflict. The PR moved on: what git left halfway is undone (it may
        have been all that was in the way), and the changes are stashed, not lost
        (commits made there stay in the worktree's reflog).
        """
        current = (await self._git(path, "rev-parse", "HEAD")).strip()
        put_on = await self._put_on(path)
        if head_sha in (current, put_on) or await self._builds_on(path, head_sha):
            if put_on != head_sha:
                await self._git(path, "update-ref", _PUT_ON, head_sha)
            return
        if abort := await self._unfinished(path):
            await self._git(path, *abort)  # back to where it began: maybe the head
            return await self._move_worktree(path, ref, head_sha)
        if await self.has_changes(path):
            message = f"purr: changes in {ref} at {current[:7]}, before {head_sha[:7]}"
            await self._stash(path, message)
        await self._git(path, "checkout", "--quiet", "--detach", "--force", head_sha)
        await self._git(path, "update-ref", _PUT_ON, head_sha)

    async def _put_on(self, path: Path) -> str | None:
        """The PR's head Purr last put the worktree on (None in one from before it kept it)."""
        try:
            return (await self._git(path, "rev-parse", "--verify", "--quiet", _PUT_ON)).strip()
        except GitError:
            return None

    async def _unfinished(self, path: Path) -> tuple[str, ...] | None:
        """What git left halfway in a worktree (stopped on a conflict, or cut short), as
        the command that aborts it, if anything."""
        git_dir = Path((await self._git(path, "rev-parse", "--absolute-git-dir")).strip())
        for left, abort in _UNFINISHED:
            if (git_dir / left).exists():
                return abort
        return None

    async def _stash(self, path: Path, message: str) -> None:
        """Stash a worktree's uncommitted changes, untracked files included."""
        await self._git(
            path,
            "-c",
            "user.name=Purr",
            "-c",
            "user.email=purr@localhost",
            "stash",
            "push",
            "--include-untracked",
            "--quiet",
            "-m",
            message,
        )

    async def _add_worktree(self, clone: Path, path: Path, head_sha: str) -> None:
        """Make a new worktree at ``path``, detached at ``head_sha``."""
        if path.exists():
            shutil.rmtree(path)  # a leftover from an interrupted checkout
        await self._git(clone, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._git(clone, "worktree", "add", "--quiet", "--detach", str(path), head_sha)
        await self._git(path, "update-ref", _PUT_ON, head_sha)

    async def _synced(self, clone: Path, ref: PRRef, head_sha: str) -> bool:
        """Whether the clone has the PR at ``head_sha`` and a recent default branch."""
        if not self._bases_fresh(ref.repo):
            return False
        return (await self._pr_heads(clone)).get(ref.number) == head_sha

    def _bases_fresh(self, repo: str) -> bool:
        """Whether the repo's default branch and PR bases were fetched recently enough."""
        return time.monotonic() - self._bases_fetched.get(repo, -BASE_REFRESH) < BASE_REFRESH

    def worktrees(self) -> list[PRRef]:
        """The PRs that have a worktree on disk."""
        found = []
        for path in sorted((self._root / "worktrees").glob("*/*/*")):
            if path.is_dir() and path.name.isdigit():
                found.append(PRRef(f"{path.parent.parent.name}/{path.parent.name}", int(path.name)))
        return found

    async def _builds_on(self, path: Path, sha: str) -> bool:
        """Whether what's checked out in the worktree has ``sha`` in its history."""
        try:
            await self._git(path, "merge-base", "--is-ancestor", sha, "HEAD")
        except GitError:
            return False
        return True

    async def has_changes(self, path: Path) -> bool:
        """Whether a worktree has uncommitted changes (ignored files don't count)."""
        return bool((await self._git(path, "status", "--porcelain")).strip())

    async def remove_worktree(self, ref: PRRef) -> bool:
        """Remove the PR's worktree, if it has one; whether it was removed.

        One with uncommitted changes (yours, or the agent's) is kept. Its review
        session survives (Claude Code keeps it under ~/.claude), and
        :meth:`checkout` makes the worktree again, at the same path, when it's needed.
        """
        path = self.worktree_path(ref)
        if not path.exists():
            return False
        if await self._has_changes_to_keep(path):
            logger.info(f"Keeping {ref}'s worktree: it has uncommitted changes")
            return False
        async with self._lock(ref.repo):
            await self._delete_worktree(self.clone_path(ref.repo), path)
        return True

    async def _has_changes_to_keep(self, path: Path) -> bool:
        """Whether a worktree has uncommitted changes; a broken one has none worth keeping."""
        if not (path / ".git").exists():
            return False
        try:
            return await self.has_changes(path)
        except GitError:
            return False  # broken: remove it

    async def _delete_worktree(self, clone: Path, path: Path) -> None:
        """Remove a worktree of ``clone``, or whatever is at its path."""
        try:
            await self._git(clone, "worktree", "remove", "--force", str(path))
        except GitError:
            shutil.rmtree(path, ignore_errors=True)  # not a worktree git knows
            if (clone / "HEAD").exists():
                await self._git(clone, "worktree", "prune")

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

    # -- Pushing what was committed in a worktree ---------------------------------------

    async def unpushed(self, ref: PRRef, onto: str, base: str = "") -> Unpushed:
        """What pushing the PR's worktree would send to its branch, at ``onto`` (the PR's
        head) now: the commits made there on top of it or, rewritten (a rebase, say), the
        branch as it'd be, from where it leaves ``base`` (the branch it merges into; "" for
        the default one), fetched first, as a rebase is likely onto its latest. A merge (of
        main, say) is one commit: what it brings is main's already. Raises GitError if
        there's no worktree, it has uncommitted changes, or a rebase is halfway."""
        path = self.worktree_path(ref)
        if not (path / ".git").exists():
            raise GitError("There's no worktree to push from.")
        if abort := await self._unfinished(path):
            raise GitError(
                f"A {abort[0]} is halfway in the worktree: finish it, or abort it "
                f"(git {' '.join(abort)}), first."
            )
        if await self.has_changes(path):
            raise GitError("The worktree has uncommitted changes: commit them first.")
        if await self._builds_on(path, onto):
            return Unpushed(await self._log(path, onto, "HEAD"))
        since = await self._fetch_base(ref.repo, base)
        replaced = await self._log(path, since, onto)
        return Unpushed(await self._log(path, since, "HEAD"), replaces=len(replaced))

    async def _log(self, path: Path, since: str, until: str) -> list[tuple[str, str]]:
        """The commits from ``since`` to ``until``, newest first: ``(sha, subject)``. A merge
        is one (``--first-parent``)."""
        log = await self._git(path, "log", "--first-parent", "--format=%H %s", f"{since}..{until}")
        commits = (line.partition(" ") for line in log.splitlines())
        return [(sha, subject) for sha, _, subject in commits]

    async def _fetch_base(self, repo: str, base: str) -> str:
        """The branch a PR merges into (``base``, or the default one), fetched now: its ref.
        If that fails, the one fetched last."""
        ref, spec = _base_ref(base), _base_refspec(base)
        clone = self.clone_path(repo)
        async with self._lock(repo):
            try:
                await self._git(clone, "fetch", "--quiet", "--no-tags", "origin", spec)
            except GitError as e:
                if not await self._has_ref(clone, ref):
                    raise
                logger.warning(f"Couldn't fetch {repo}'s {base or 'default branch'}: {e}")
        return ref

    async def push(self, ref: PRRef, commit: str, *, repo: str, branch: str, onto: str) -> None:
        """Push ``commit`` to ``branch`` in ``repo`` (the PR's head), only while that branch
        is still at ``onto`` (a lease: nobody pushed since). On top of it, or in its place
        (a rebase): the lease lets git replace the branch. Raises GitError if refused."""
        clone = self.clone_path(ref.repo)
        async with self._lock(ref.repo):
            origin = (await self._git(clone, "remote", "get-url", "origin")).strip()
            await self._git(
                clone,
                "push",
                "--quiet",
                f"--force-with-lease=refs/heads/{branch}:{onto}",
                push_url(origin, ref.repo, repo),
                f"{commit}:refs/heads/{branch}",
            )

    # -- Open PRs, for their diffs ------------------------------------------------------

    @staticmethod
    def _range(pr: PR) -> str:
        """What GitHub diffs: from where the PR branched off its base to its head."""
        return f"{_base_ref(pr.base)}...{PR_REFS}/{pr.number}"

    async def sync(self, repo: str, prs: list[PR]) -> None:
        """Bring the repo's open PRs (``prs``) into the clone, so their diffs are local.

        Only PRs that are new or have new commits are fetched, and only the
        files their diffs touch. PRs no longer open lose their refs.
        """
        async with self._lock(repo):
            clone = await self._clone(repo)
            have = await self._pr_heads(clone)
            moved = [pr for pr in prs if have.get(pr.number) != pr.head_sha]
            gone = set(have) - {pr.number for pr in prs}
            if not moved and not gone and self._bases_fresh(repo):
                return  # nothing new: no network
            bases = sorted({pr.base for pr in prs if pr.base})
            specs = [_BASE_REFSPEC, *map(_base_refspec, bases)]
            specs += [_pr_refspec(pr.number) for pr in moved]
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
                await self._fetch_one(clone, spec)

    async def _fetch_one(self, clone: Path, spec: str) -> None:
        try:
            await self._git(clone, "fetch", "--quiet", "--no-tags", "origin", spec)
        except GitError as e:
            logger.warning(f"Couldn't fetch {spec}: {e}")

    async def _fetch_blobs(self, clone: Path, prs: list[PR]) -> None:
        """Fetch the files these PRs' diffs touch that the clone doesn't have, in one go."""
        wanted: set[str] = set()
        for pr in prs:
            wanted |= await self._blobs_touched(clone, pr)
        missing = await self._missing(clone, wanted)
        if not missing:
            return
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

    async def _blobs_touched(self, clone: Path, pr: PR) -> set[str]:
        """The files the PR's diff touches, before and after; none if git can't tell."""
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
            return set()
        return _blobs(raw)

    async def _missing(self, clone: Path, objects: set[str]) -> list[str]:
        """Which of these objects the clone doesn't have."""
        if not objects:
            return []
        check = await self._git(
            clone,
            "cat-file",
            "--batch-check=%(objectname)",
            stdin="\n".join(objects) + "\n",
            env=_NO_LAZY_FETCH,
        )
        return [line.split()[0] for line in check.splitlines() if line.endswith(" missing")]

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
        async with self._lock(pr.repo):
            try:
                await self._fetch_head(clone, pr)
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

    async def numstat(self, pr: PR) -> list[FileChange] | None:
        """The files the PR changes, with lines added and removed, from the clone (what the
        sync brought: no network); None if the clone can't tell (yet)."""
        clone = self.clone_path(pr.repo)
        if not (clone / "HEAD").exists():
            return None
        try:
            stat = await self._git(
                clone, "diff", "--numstat", "--no-renames", self._range(pr), env=_NO_LAZY_FETCH
            )
        except GitError:
            return None
        return [_file_change(line) for line in stat.splitlines() if line.count("\t") >= 2]

    async def _fetch_head(self, clone: Path, pr: PR) -> None:
        """Fetch the PR's head, unless the clone has it already."""
        if (await self._pr_heads(clone)).get(pr.number) != pr.head_sha:
            await self._git(clone, "fetch", "--quiet", "origin", _pr_refspec(pr.number))

    async def local(self, pr: PR) -> Local | None:
        """What the agent committed in the PR's worktree that isn't on GitHub yet: commits
        on top of the PR's head, or the PR rewritten (a rebase). None if there's nothing,
        git's halfway through something there, or the worktree is on an older head (the
        PR moved on since)."""
        path = self.worktree_path(pr.ref)
        if not (path / ".git").exists():
            return None
        try:
            head = (await self._git(path, "rev-parse", "HEAD")).strip()
            if head == pr.head_sha or await self._unfinished(path):
                return None
            if await self._builds_on(path, pr.head_sha):
                return Local(head, len(await self._log(path, pr.head_sha, "HEAD")))
            if await self._put_on(path) == pr.head_sha:
                return Local(head, 0)
        except GitError:
            return None
        return None

    async def local_diff(self, pr: PR, local: Local) -> str | None:
        """The PR's diff as it'll be once what's ``local`` is pushed; None if git can't
        tell. A rebase, or a merge, likely brings in the base's latest: it's fetched first
        (and if the clone doesn't have the base yet)."""
        clone = self.clone_path(pr.repo)
        base = _base_ref(pr.base)
        try:
            merges = await self._git(clone, "rev-list", "--merges", f"{pr.head_sha}..{local.head}")
            if not local.commits or merges.strip() or not await self._has_ref(clone, base):
                base = await self._fetch_base(pr.repo, pr.base or "")
            return await self._git(clone, *_DIFF, f"{base}...{local.head}")
        except GitError:
            return None

    async def moved(self, pr: PR, local: Local) -> str:
        """How the agent's commits changed the PR's files (a diff with no context): where
        lines of the PR's head are now. Empty if git can't tell."""
        try:
            changes = ("diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames")
            return await self._git(self.clone_path(pr.repo), *changes, pr.head_sha, local.head)
        except GitError:
            return ""

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
            return await self._git(clone, *_DIFF, self._range(pr), env=_NO_LAZY_FETCH)
        except GitError:
            return None

    async def file_at(self, pr: PR, path: str, commit: str = "") -> str | None:
        """A file as it is at the PR's head (or ``commit``), from the clone; None if the
        clone doesn't have that commit (yet), or the file."""
        clone = self.clone_path(pr.repo)
        if not (clone / "HEAD").exists():
            return None
        try:
            at = commit or pr.head_sha
            return await self._git(clone, "show", f"{at}:{path}", env=_NO_LAZY_FETCH)
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
        async with self._lock(repo):
            clone = await self._clone(repo)
            if not await self._has_ref(clone, BASE_REF):
                await self._git(clone, "fetch", "--quiet", "origin", _BASE_REFSPEC)
            trees = await self._plugin_trees(clone)
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
            for old in [version for version in versions.iterdir() if version != path]:
                shutil.rmtree(old, ignore_errors=True)
            return path

    async def _plugin_trees(self, clone: Path) -> dict[str, str]:
        """The plugin's parts on the default branch: ``.claude/skills`` -> its tree's hash."""
        listing = await self._git(clone, "ls-tree", BASE_REF, ".claude/")
        trees = {}
        for line in listing.splitlines():
            info, _, path = line.partition("\t")
            _, kind, sha = info.split()
            if kind == "tree" and path.removeprefix(".claude/") in PLUGIN_PARTS:
                trees[path] = sha
        return trees

    @staticmethod
    def _unpack_plugin(repo: str, archive: bytes, path: Path) -> None:
        staging = path.with_name(f"{path.name}.tmp")
        shutil.rmtree(staging, ignore_errors=True)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(staging, filter="data")
        plugin = staging / ".claude"
        markdowns = [md for md in plugin.rglob("*.md") if md.is_file() and not md.is_symlink()]
        for markdown in markdowns:
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
        return await self._run("git", "-C", str(cwd), *args, stdin=stdin, env=env)

    @staticmethod
    async def _run(*args: str, stdin: str | None = None, env: dict | None = None) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **env} if env else None,
            )
        except OSError as e:  # git isn't installed, say
            raise GitError(f"Couldn't run {_command(args)}: {e}") from e
        stdout, stderr = await process.communicate(stdin.encode() if stdin is not None else None)
        if process.returncode != 0:
            raise GitError(f"{_command(args)} failed: {stderr.decode().strip()}")
        return stdout
