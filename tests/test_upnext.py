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
    assert "." not in KNOWN.folders  # the top folder: README, pyproject.toml, ...
    assert KNOWN.authors == {"bob": 2}  # the PRs of theirs you reviewed, not yours
    # What most PRs touch says nothing, once there are enough to tell.
    many = [PastPR("me", ("changelog/1.md", f"src/{n % 4}/x.py"), yours=True) for n in range(12)]
    assert set(Known.of(many).folders) == {"src/0", "src/1", "src/2", "src/3"}


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
    assert why(small).startswith(
        "Small (12 lines); in services/deepgram, where you've been in 2 PRs"
    )
    assert reasons(old) == ["waiting 23 days"]
    assert why(big) == "900 lines in 1 file"  # nothing stands out


def test_up_next_comes_from_the_pool():
    pool = pr(1)
    assert candidates([pool]) == [pool]
    assert candidates([replace(pool, assignees=("alice",))]) == []  # taken
    assert candidates([replace(pool, state=PRState.DONE)]) == []
    assert candidates([replace(pool, requested_from_me=True)]) == []  # a group of its own


def test_what_the_lead_picks_and_is_told():
    from purr.models import PRRef, UpNext
    from purr.upnext import notes, picks_from

    small = signals(pr(1), [FileChange(f"{DEEPGRAM}/stt.py", 8, 4)], KNOWN, NOW)
    big = signals(pr(2, "carol"), [FileChange("src/x.py", 900, 0)], KNOWN, NOW)
    told = notes("pipecat-ai/pipecat", [small, big], instructions="Skip docs.", skipped=[pr(9)])
    assert "- #1 “PR 1” by bob, opened 1 days ago: 12 lines in 1 files; small (12 lines)" in told
    assert "weren't for them:\n- #9" in told and told.endswith("Skip docs.")

    output = {"picks": [{"pr": "#2", "why": "Big."}, {"pr": "7", "why": "?"}, {"pr": "a/b#1"}]}
    ref = PRRef("pipecat-ai/pipecat", 2)
    assert picks_from(output, [small, big], count=1) == [UpNext(ref, "Big.", 0, by="lead")]
    assert [p.pr.number for p in picks_from(output, [small, big], count=5)] == [2, 1]  # not #7


async def test_up_next_is_the_leads_pick_from_the_pool(tmp_path):
    from purr.models import PRRef
    from purr.store import Store
    from purr.workers import UpNextWorker
    from tests.fakes import FakeGitHub, FakePicker, FakeRepos, remote

    repo = "pipecat-ai/pipecat"
    store = await Store.open(":memory:")
    await store.add_repo(repo)
    for number in (1, 2, 3):
        await store.upsert_pr(remote(number, created_at="2026-11-20T00:00:00Z").to_pr("me"))
    await store.upsert_pr(remote(4, assignees=("alice",)).to_pr("me"))  # taken
    github, repos, picker = FakeGitHub(), FakeRepos(tmp_path), FakePicker()
    github.past = [PastPR("me", (f"{DEEPGRAM}/stt.py",), yours=True)]
    repos.stats = {
        PRRef(repo, 1): [FileChange("src/big.py", 900, 0)],
        PRRef(repo, 2): [FileChange(f"{DEEPGRAM}/stt.py", 3, 1)],
        PRRef(repo, 4): [FileChange("x.py", 1, 0)],
    }  # #3: the clone doesn't have it yet
    worker = UpNextWorker(store=store, github=github, repos=repos, picker=picker, count=1)

    assert await worker.pick(repo)
    [told] = picker.notes
    assert told.index("- #2 ") < told.index("- #1 ") and "- #3 " not in told and "- #4 " not in told
    assert "where you've been in 1 PRs" in told
    assert [(u.pr.number, u.why, u.by) for u in await store.up_next(repo)] == [
        (2, "Why #2", "lead")
    ]

    # The same shortlist: the lead isn't asked again.
    assert not await worker.pick(repo)
    # The lead can't say: the top of the shortlist, with its reasons.
    repos.stats[PRRef(repo, 3)] = [FileChange("docs/a.md", 1, 1)]
    picker.fail_with, worker._ask_every = "offline", 0
    assert await worker.pick(repo)
    [pick] = await store.up_next(repo)
    assert pick.by == "purr" and pick.why.startswith("Small (")

    # Someone took it: it's no longer up next.
    await store.upsert_pr(remote(pick.pr.number, assignees=("alice",)).to_pr("me"))
    assert await store.up_next(repo) == []
    await store.close()


async def test_skipping_and_taking_teach_the_lead(tmp_path):
    from purr.models import PRRef
    from purr.store import Store
    from purr.workers import UpNextWorker
    from tests.fakes import FakeGitHub, FakePicker, FakeRepos, remote

    repo = "pipecat-ai/pipecat"
    store = await Store.open(":memory:")
    await store.add_repo(repo)
    for number in (1, 2, 3):
        await store.upsert_pr(remote(number, title=f"Fix {number}").to_pr("me"))
    repos, picker = FakeRepos(tmp_path), FakePicker()
    repos.stats = {PRRef(repo, n): [FileChange("x.py", n, 0)] for n in (1, 2, 3)}
    worker = UpNextWorker(
        store=store, github=FakeGitHub(), repos=repos, picker=picker, count=2, ask_every=0
    )
    await worker.pick(repo)
    assert [u.pr.number for u in await store.up_next(repo)] == [1, 2]

    # You took #2 (reviewed it), and #1 isn't for you.
    await store.start_review(PRRef(repo, 2))
    await store.skip_up_next(PRRef(repo, 1))
    assert [u.pr.number for u in await store.up_next(repo)] == [2]
    await worker.pick(repo)
    told = picker.notes[-1]
    assert "- #1 " not in told.split("The user")[0]  # never offered again
    assert "lately took:\n- #2 “Fix 2”" in told and "weren't for them:\n- #1 “Fix 1”" in told
    await store.close()


async def test_up_next_stays_full(tmp_path):
    from purr.models import PRRef
    from purr.store import Store
    from purr.upnext import fill
    from purr.workers import UpNextWorker
    from tests.fakes import FakeGitHub, FakePicker, FakeRepos, remote

    repo = "pipecat-ai/pipecat"
    store = await Store.open(":memory:")
    await store.add_repo(repo)
    for number in (1, 2, 3, 4):
        await store.upsert_pr(remote(number).to_pr("me"))
    repos, picker = FakeRepos(tmp_path), FakePicker()
    repos.stats = {PRRef(repo, n): [FileChange("x.py", n, 0)] for n in (1, 2, 3, 4)}
    worker = UpNextWorker(store=store, github=FakeGitHub(), repos=repos, picker=picker, count=2)

    # The lead picks fewer than asked: the rest comes from the top of the shortlist.
    picker.choose = [3]
    await worker.pick(repo)
    assert [(u.pr.number, u.by) for u in await store.up_next(repo)] == [(3, "lead"), (1, "purr")]

    # You take one: its place is filled at once, however recently the lead was asked.
    await store.upsert_pr(remote(3, assignees=("me",)).to_pr("me"))
    picker.choose = None
    assert await worker.pick(repo)
    assert len(picker.notes) == 2
    assert [u.pr.number for u in await store.up_next(repo)] == [1, 2]
    # Full, and the pool changes: the lead isn't asked again so soon.
    await store.upsert_pr(remote(5).to_pr("me"))
    repos.stats[PRRef(repo, 5)] = [FileChange("y.py", 1, 0)]
    assert not await worker.pick(repo) and len(picker.notes) == 2

    # Fewer in the pool than places: all of it.
    short = [signals(pr(n), [FileChange("x.py", 1, 0)], Known(), NOW) for n in (7, 8)]
    assert [u.pr.number for u in fill([], short, count=5)] == [7, 8]
    await store.close()
