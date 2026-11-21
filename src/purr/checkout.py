"""Which GitHub repo a local checkout belongs to, for launching ``purr`` inside one."""

import asyncio
from pathlib import Path

from purr.models import parse_repo_name

# PRs live upstream when working from a fork, so look there first.
_REMOTE_PREFERENCE = ("upstream", "origin")


def _parse_remotes(output: str) -> list[str]:
    """GitHub repos from ``git remote -v`` output, most likely PR home first."""
    urls: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in urls:
            urls[parts[0]] = parts[1]
    ordered = [r for r in _REMOTE_PREFERENCE if r in urls] + sorted(
        r for r in urls if r not in _REMOTE_PREFERENCE
    )
    repos: list[str] = []
    for remote in ordered:
        try:
            repo = parse_repo_name(urls[remote])
        except ValueError:
            continue  # not GitHub
        if repo not in repos:
            repos.append(repo)
    return repos


async def github_repos(path: Path) -> list[str]:
    """GitHub repos the git checkout at ``path`` points at (empty if it isn't one)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(path),
            "remote",
            "-v",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return []
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return []
    return _parse_remotes(stdout.decode())
