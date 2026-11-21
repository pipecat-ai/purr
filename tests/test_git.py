"""Worktrees from a local "GitHub" repo that has PR refs, like GitHub's refs/pull/N/head."""

import subprocess
from pathlib import Path

import pytest

from purr.git import GitError, GitRepos
from purr.models import PRRef


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path) -> Path:
    """A repo with a main branch and PR 7's head published at refs/pull/7/head."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "one")
    git(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("two\n")
    git(repo, "commit", "-qam", "two")
    git(repo, "update-ref", "refs/pull/7/head", "HEAD")
    git(repo, "checkout", "-q", "main")
    return repo


def local_clone(upstream: Path):
    return lambda repo, dest: ["git", "clone", "--quiet", "--bare", str(upstream), str(dest)]


async def test_checkout_makes_a_worktree_at_the_pr_head(tmp_path, upstream):
    repos = GitRepos(tmp_path / "data", clone_command=local_clone(upstream))
    ref = PRRef("a/b", 7)
    head = git(upstream, "rev-parse", "refs/pull/7/head")

    path = await repos.checkout(ref, head)
    assert path == tmp_path / "data/worktrees/a/b/7"
    assert (path / "a.txt").read_text() == "two\n"
    assert git(path, "rev-parse", "HEAD") == head

    # New commits on the PR: checking out again moves the same worktree.
    git(upstream, "checkout", "-q", "feature")
    (upstream / "a.txt").write_text("three\n")
    git(upstream, "commit", "-qam", "three")
    git(upstream, "update-ref", "refs/pull/7/head", "HEAD")
    new_head = git(upstream, "rev-parse", "HEAD")
    assert await repos.checkout(ref, new_head) == path
    assert (path / "a.txt").read_text() == "three\n"


async def test_unknown_pr_fails(tmp_path, upstream):
    repos = GitRepos(tmp_path / "data", clone_command=local_clone(upstream))
    with pytest.raises(GitError):
        await repos.checkout(PRRef("a/b", 99), "0" * 40)


async def test_guidelines_come_from_the_default_branch(tmp_path, upstream):
    (upstream / "CLAUDE.md").write_text("@AGENTS.md\n")
    (upstream / "AGENTS.md").write_text("Use Google-style docstrings.\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "guidelines")
    # The PR tries to relax them; reviews still see main's.
    git(upstream, "checkout", "-q", "feature")
    (upstream / "AGENTS.md").write_text("Anything goes.\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "relax")
    git(upstream, "update-ref", "refs/pull/7/head", "HEAD")
    git(upstream, "checkout", "-q", "main")

    repos = GitRepos(tmp_path / "data", clone_command=local_clone(upstream))
    await repos.checkout(PRRef("a/b", 7), git(upstream, "rev-parse", "refs/pull/7/head"))
    guidelines = await repos.guidelines("a/b")
    assert guidelines == "### AGENTS.md\n\nUse Google-style docstrings."


async def test_no_guidelines(tmp_path, upstream):
    repos = GitRepos(tmp_path / "data", clone_command=local_clone(upstream))
    await repos.checkout(PRRef("a/b", 7), git(upstream, "rev-parse", "refs/pull/7/head"))
    assert await repos.guidelines("a/b") == ""
