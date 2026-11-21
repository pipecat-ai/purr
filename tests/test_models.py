import pytest

from purr.models import PRRef


def test_pr_ref_round_trip():
    ref = PRRef.parse("pipecat-ai/pipecat#2841")
    assert ref == PRRef("pipecat-ai/pipecat", 2841)
    assert str(ref) == "pipecat-ai/pipecat#2841"


@pytest.mark.parametrize("value", ["pipecat#2841", "pipecat-ai/pipecat", "a/b#x", "a/b/c#1"])
def test_pr_ref_rejects_malformed(value):
    with pytest.raises(ValueError):
        PRRef.parse(value)
