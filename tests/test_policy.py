"""Purr's one permission rule, on top of Claude Code's: nothing that changes GitHub."""

import pytest

from purr.review.policy import GITHUB_DENY_RULES, github_write


@pytest.mark.parametrize(
    "command",
    [
        "gh pr review 12 --approve",
        "gh pr comment 12 --body 'nice'",
        "gh pr merge 12 --squash",
        "gh issue close 3",
        "gh api -X POST repos/a/b/pulls/12/reviews",
        "gh api --method=DELETE repos/a/b/issues/comments/1",
        "gh api repos/a/b/issues/12/comments -f body=hi",
        "git push origin HEAD",
        "git -C ../other push",
        "echo ok && gh pr merge 12",
        "bash -c 'gh pr review 12 --approve'",
        'echo "$(gh pr merge 12)"',
        "gh auth token",
        "cat ~/.config/gh/hosts.yml",
    ],
)
def test_github_writes_and_your_token_are_refused(command):
    assert github_write("Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 12 --repo pipecat-ai/pipecat",
        "gh pr diff 12 | head -200",
        "gh api repos/pipecat-ai/pipecat/pulls/12/comments",
        "git log --grep push",
        "git -C /data/worktrees/a/b/1 diff 834903d HEAD",
        "uv run pytest",
        "rm -rf build",  # Claude Code's permissions decide this one, not Purr's rule
    ],
)
def test_everything_else_is_up_to_claude_code(command):
    assert github_write("Bash", {"command": command}) is None


def test_tools():
    assert github_write("Read", {"file_path": "/home/me/.config/gh/hosts.yml"})
    assert github_write("Read", {"file_path": "src/a.py"}) is None
    assert github_write("Edit", {"file_path": "src/a.py"}) is None
    assert github_write("WebFetch", {"url": "https://github.com"}) is None
    assert "Bash(gh pr merge:*)" in GITHUB_DENY_RULES
    assert "Bash(gh api:*)" not in GITHUB_DENY_RULES  # its reads are fine
