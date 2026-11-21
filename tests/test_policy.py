import pytest

from purr.review.policy import Decision, Policy


@pytest.fixture
def policy(tmp_path):
    return Policy(tmp_path / "worktree")


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 12 --repo pipecat-ai/pipecat",
        "gh pr diff 12 --repo pipecat-ai/pipecat | head -200",
        "gh api repos/pipecat-ai/pipecat/pulls/12/comments",
        "git log --oneline -20",
        "git --no-pager diff HEAD~1",
        "git log --grep push",
        "rg -n 'def subscribe' src",
        "find . -name '*.py'",
        "cat src/pipecat/bus/bus.py",
        "ls -la",
        "gh pr view 6924 --repo pipecat-ai/pipecat; gh pr diff 6924 --repo pipecat-ai/pipecat",
        "git show --stat HEAD && git log --oneline -3 HEAD",
        "grep -n x uv.lock | head -40; ls changelog",
        "git log 2>/dev/null | head",
        "rg foo 2>&1 | head -5",
        'git grep -n -E "setup-uv|uv-version|astral-sh" -- .github pyproject.toml',
        'git ls-files changelog/ | git grep -l -i "sip\\|baresip" -- changelog/',
        'grep -n "x" pyproject.toml; git show main:a.toml 2>/dev/null | grep -n "a\\|^sip";',
        "git log --oneline -3; echo ---; grep -rn baresip --include='*.toml' .",
        "git status\ngit log -1",
    ],
)
def test_read_only_commands_run_without_asking(policy, command):
    assert policy.check("Bash", {"command": command}).decision == Decision.ALLOW


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
        "gh auth token",
    ],
)
def test_github_writes_are_always_denied(policy, command):
    verdict = policy.check("Bash", {"command": command})
    assert verdict.decision == Decision.DENY
    assert "propose_action" in verdict.reason


@pytest.mark.parametrize(
    "command",
    [
        "uv run pytest tests/test_bus.py",
        "python setup.py",
        "make test",
        "cat ~/.ssh/id_rsa",
        "cat ../../secrets.txt",
        "find . -name '*.pyc' -delete",
        "git checkout main",
        "cd src && ls",
        "curl https://example.com",
        "git log > out.txt",
        "ls && rm -rf build",
        "cat x | sh",
        "ls & curl https://example.com",
        "echo $(cat ~/.netrc)",
        "ls `whoami`",
        "ls >/dev/nullx",
        "ls\nrm -rf build",
        "grep x <<EOF",
        "(cd .. && ls)",
        "git log >> notes.txt",
        "echo 'unterminated",
    ],
)
def test_everything_else_asks(policy, command):
    assert policy.check("Bash", {"command": command}).decision == Decision.ASK


def test_tools(policy, tmp_path):
    worktree = tmp_path / "worktree"
    assert (
        policy.check("Read", {"file_path": str(worktree / "src/a.py")}).decision == Decision.ALLOW
    )
    assert policy.check("Read", {"file_path": "src/a.py"}).decision == Decision.ALLOW
    assert policy.check("Read", {"file_path": "/etc/passwd"}).decision == Decision.ASK
    assert policy.check("Grep", {"pattern": "x"}).decision == Decision.ALLOW
    assert policy.check("Edit", {"file_path": "src/a.py"}).decision == Decision.ASK
    assert policy.check("WebFetch", {"url": "https://x"}).decision == Decision.ASK
    assert policy.check("mcp__purr__submit_review", {}).decision == Decision.ALLOW


def test_skills_and_their_files(tmp_path):
    plugin = tmp_path / "plugins/pipecat"
    policy = Policy(tmp_path / "worktree", readable=[plugin])
    assert policy.check("Skill", {"skill": "pipecat:docstring"}).decision == Decision.ALLOW
    skill_file = str(plugin / "skills/docstring/SKILL.md")
    assert policy.check("Read", {"file_path": skill_file}).decision == Decision.ALLOW
    assert policy.check_command(f"cat {skill_file}").decision == Decision.ALLOW
    assert policy.check("Read", {"file_path": str(tmp_path / "x")}).decision == Decision.ASK
