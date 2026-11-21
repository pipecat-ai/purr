from purr.checkout import _parse_remotes, github_repos

REMOTES = """\
origin\tgit@github.com:aconchillo/pipecat.git (fetch)
origin\tgit@github.com:aconchillo/pipecat.git (push)
gitlab\thttps://gitlab.com/someone/pipecat.git (fetch)
upstream\thttps://github.com/pipecat-ai/pipecat.git (fetch)
upstream\thttps://github.com/pipecat-ai/pipecat.git (push)
"""


def test_upstream_comes_first_and_non_github_is_ignored():
    assert _parse_remotes(REMOTES) == ["pipecat-ai/pipecat", "aconchillo/pipecat"]


async def test_not_a_checkout(tmp_path):
    assert await github_repos(tmp_path) == []
