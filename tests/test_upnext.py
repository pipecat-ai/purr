"""What makes a PR in the pool worth looking at next (purr.upnext): no GitHub, no Claude."""

from dataclasses import replace
from datetime import UTC, datetime

from purr.models import PR, FileChange, PastPR, PRState
from purr.upnext import Known, candidates, reasons, score, shortlist, signals, why

NOW = datetime(2026, 11, 24, tzinfo=UTC)


def pr(number: int, author: str = "bob", created_at: str = "2026-11-23T00:00:00Z", **kw) -> PR:
    return PR(
        "pipecat-ai/pipecat", number, f"PR {number}", author, "u", "abc", created_at,
        created_at=created_at, **kw,
    )  # fmt: skip


DEEPGRAM = "src/pipecat/services/deepgram"
KNOWN = Known.of(
    [
        PastPR("me", (f"{DEEPGRAM}/stt.py", f"{DEEPGRAM}/tts.py"), yours=True),
        PastPR("bob", (f"{DEEPGRAM}/stt.py", "README.md"), yours=False),
        PastPR("bob", ("src/pipecat/frames/frames.py",), yours=False),
    ]
)


def test_what_you_know_of_a_repo():
    assert KNOWN.folders[DEEPGRAM] == 2  # once per PR, however many files
    assert KNOWN.folders["."] == 1
    assert KNOWN.authors == {"bob": 2}  # the PRs of theirs you reviewed, not yours


def test_small_and_where_youve_worked_comes_first():
    small = signals(pr(1), [FileChange(f"{DEEPGRAM}/stt.py", 8, 4)], KNOWN, NOW)
    big = signals(pr(2, "carol"), [FileChange("src/pipecat/new/x.py", 900, 0)], KNOWN, NOW)
    old = signals(
        pr(3, "carol", "2026-11-01T00:00:00Z"), [FileChange("src/x.py", 900, 0)], KNOWN, NOW
    )
    assert (small.lines, small.files, small.known_folder, small.known) == (12, 1, DEEPGRAM, 2)
    assert small.by_known == 2 and big.known == 0 and big.known_folder == ""
    assert score(small) > score(old) > score(big)  # waiting counts, a little
    assert [s.pr.number for s in shortlist([big, old, small], size=2)] == [1, 3]

    assert reasons(small) == [
        "small (12 lines)",
        "in services/deepgram, where you've been in 2 PRs",
        "you've reviewed 2 of bob's PRs",
    ]
    assert why(small).startswith("Small (12 lines); in services/deepgram")
    assert reasons(old) == ["waiting 23 days"]
    assert why(big) == "900 lines in 1 file"  # nothing stands out


def test_up_next_comes_from_the_pool():
    pool = pr(1)
    assert candidates([pool]) == [pool]
    assert candidates([replace(pool, authored=True)]) == []  # yours
    assert candidates([replace(pool, assignees=("alice",))]) == []  # taken
    assert candidates([replace(pool, state=PRState.DONE)]) == []
    assert candidates([replace(pool, requested_from_me=True)]) == []  # a group of its own
