from purr.review.instructions import Kind, ReviewInstructions


def test_review_and_code_style(tmp_path):
    instructions = ReviewInstructions(tmp_path)
    assert instructions.for_repo("pipecat-ai/pipecat") == ""

    # New files start with a how-to that isn't sent to the agent.
    review = instructions.ensure(Kind.REVIEW)
    assert "How you like reviews" in review.read_text()
    assert instructions.for_repo("pipecat-ai/pipecat") == ""

    review.write_text(review.read_text() + "- Be terse.\n")
    code = instructions.ensure(Kind.CODE)
    code.write_text("- Keep functions short.\n")
    instructions.ensure(Kind.CODE, "pipecat-ai/pipecat").write_text(
        "- Services follow the patterns.\n"
    )
    assert instructions.path(Kind.CODE, "pipecat-ai/pipecat") == (
        tmp_path / "repos/pipecat-ai/pipecat/code.md"
    )

    text = instructions.for_repo("pipecat-ai/pipecat")
    assert "## How the user likes reviews\n\n- Be terse." in text
    assert "How the user likes code (hold the PR to this" in text
    assert "- Keep functions short." in text
    assert "in pipecat-ai/pipecat\n\n- Services follow the patterns." in text
    assert "<!--" not in text
    assert "Services follow" not in instructions.for_repo("pipecat-ai/whisker")
