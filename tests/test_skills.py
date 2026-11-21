"""Spotting a repo's own review skill among its skills."""

from purr.review.agent import rereview_prompt, review_prompt
from purr.review.skills import Skill, review_skills, skill_section
from tests.fakes import make_plugin
from tests.test_reviews import make_pr

# pipecat's skills, as of November 2026.
PIPECAT = {
    "code-review": {
        "name": "code-review",
        "description": "Automated code review for pull requests using multiple specialized agents",
        "disable-model-invocation": True,
    },
    "prose-review": "Check that added comments, docstrings, and changelog entries document "
    "the code rather than narrate the change that produced it",
    "cleanup": "Review, refactor, document, and validate code changes in the current branch",
    "pr-description": "Draft or update a GitHub PR description that explains the problem, "
    "changed behavior, and review concerns in plain language.",
    "changelog": "Create changelog files for important commits in a PR",
    "pr-submit": "Create and submit a GitHub PR from the current branch",
}


def test_pipecats_code_review_is_clearly_its_review_skill(tmp_path):
    skills = review_skills(make_plugin(tmp_path, PIPECAT))
    assert [(s.name, s.likely) for s in skills] == [
        ("pipecat:code-review", True),
        ("pipecat:prose-review", False),  # a review, but it doesn't say of PRs
    ]


def test_what_counts_as_a_review_skill(tmp_path):
    plugin = make_plugin(
        tmp_path,
        {
            "audit": "Reviews the current PR for security problems",  # says so, name doesn't
            "review": "Review a pull request",
            "hidden-review": {"description": "Code review", "user-invocable": False},
            "named": {"name": "pr-review", "description": "PR review, the team's way"},
        },
        name="acme",
        commands={"review-pr": "Review the given PR", "deploy": "Deploy it"},
    )
    assert review_skills(plugin) == [
        Skill("acme:pr-review", "PR review, the team's way", likely=True),
        Skill("acme:review", "Review a pull request", likely=True),
        Skill("acme:review-pr", "Review the given PR", likely=True),
        Skill("acme:audit", "Reviews the current PR for security problems", likely=False),
    ]


def test_no_plugin_no_skills(tmp_path):
    assert review_skills(None) == []
    assert review_skills(tmp_path) == []  # no manifest


def test_a_review_following_a_skill_starts_with_it():
    pr = make_pr()
    assert review_prompt(pr, "pipecat:code-review") == f"/pipecat:code-review {pr.url}"
    assert review_prompt(pr).startswith("Review pipecat-ai/pipecat#2841")
    assert "following the pipecat:code-review skill's process" in rereview_prompt(
        "a", "b", "pipecat:code-review"
    )
    section = skill_section("pipecat-ai/pipecat", "pipecat:code-review")
    assert "`/pipecat:code-review`" in section and "propose_action" in section
