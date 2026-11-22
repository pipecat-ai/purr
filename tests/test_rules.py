"""Your "always allow" answers, kept as Claude Code rules."""

import json

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import PermissionRuleValue, PermissionUpdate

from purr.review.agent import PermissionAnswer, permission_result
from purr.review.rules import RepoRules, rule_texts

REPO = "pipecat-ai/pipecat"
SUGGESTED = [
    PermissionUpdate(
        type="addRules",
        rules=[PermissionRuleValue("Bash", "uv run pytest:*")],
        behavior="allow",
        destination="localSettings",
    )
]


def test_rule_texts():
    assert rule_texts(SUGGESTED) == ["Bash(uv run pytest:*)"]
    whole_tool = PermissionUpdate(
        type="addRules", rules=[PermissionRuleValue("WebFetch")], behavior="allow"
    )
    assert rule_texts([whole_tool]) == ["WebFetch"]
    assert rule_texts([PermissionUpdate(type="setMode", mode="acceptEdits")]) == []


def test_repo_rules_and_what_reviews_run_with(tmp_path):
    rules = RepoRules(tmp_path)
    assert rules.rules(REPO) == []
    rules.add(["Bash(uv run pytest:*)"], REPO)
    rules.add(["Bash(uv run pytest:*)", "WebFetch"], REPO)  # once each
    assert rules.rules(REPO) == ["Bash(uv run pytest:*)", "WebFetch"]
    assert rules.rules("pipecat-ai/whisker") == []

    settings = json.loads(rules.settings(REPO))
    assert settings["permissions"]["allow"] == ["Bash(uv run pytest:*)", "WebFetch"]
    assert "Bash(gh pr merge:*)" in settings["permissions"]["deny"]
    assert "sandbox" not in settings
    sandboxed = json.loads(rules.settings(REPO, sandbox=True))["sandbox"]
    assert sandboxed["enabled"] and "api.github.com" in sandboxed["network"]["allowedDomains"]

    rules.path(REPO).write_text("{broken")  # edited by hand, badly: ignored, not fatal
    assert rules.rules(REPO) == []


def test_answers_become_claude_codes_permission_results(tmp_path):
    rules = RepoRules(tmp_path)

    once = permission_result(PermissionAnswer(True), SUGGESTED, REPO, rules)
    assert isinstance(once, PermissionResultAllow) and once.updated_permissions is None

    everywhere = permission_result(PermissionAnswer(True, "all"), SUGGESTED, REPO, rules)
    assert [u.destination for u in everywhere.updated_permissions] == ["userSettings"]
    assert rules.rules(REPO) == []  # it's in your Claude Code settings, not Purr's

    this_repo = permission_result(PermissionAnswer(True, "repo"), SUGGESTED, REPO, rules)
    assert [u.destination for u in this_repo.updated_permissions] == ["session"]
    assert rules.rules(REPO) == ["Bash(uv run pytest:*)"]  # for the repo's next reviews

    no = permission_result(PermissionAnswer(False), SUGGESTED, REPO, rules)
    away = permission_result(PermissionAnswer(None), SUGGESTED, REPO, rules)
    assert isinstance(no, PermissionResultDeny) and "said no" in no.message
    assert isinstance(away, PermissionResultDeny) and "didn't answer" in away.message
