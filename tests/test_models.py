import pytest

from purr.models import PRRef, parse_repo_name


def test_pr_ref_round_trip():
    ref = PRRef.parse("pipecat-ai/pipecat#2841")
    assert ref == PRRef("pipecat-ai/pipecat", 2841)
    assert str(ref) == "pipecat-ai/pipecat#2841"


@pytest.mark.parametrize("value", ["pipecat#2841", "pipecat-ai/pipecat", "a/b#x", "a/b/c#1"])
def test_pr_ref_rejects_malformed(value):
    with pytest.raises(ValueError):
        PRRef.parse(value)


@pytest.mark.parametrize(
    "value",
    [
        "pipecat-ai/pipecat",
        "https://github.com/pipecat-ai/pipecat",
        "https://github.com/pipecat-ai/pipecat/",
        "github.com/pipecat-ai/pipecat.git",
        "git@github.com:pipecat-ai/pipecat.git",
        "  pipecat-ai/pipecat  ",
    ],
)
def test_parse_repo_name(value):
    assert parse_repo_name(value) == "pipecat-ai/pipecat"


@pytest.mark.parametrize("value", ["pipecat", "https://gitlab.com/a/b", "a/b/c", ""])
def test_parse_repo_name_rejects_others(value):
    with pytest.raises(ValueError):
        parse_repo_name(value)
