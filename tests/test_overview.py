"""PR details: the cache in front of GitHub, and how they're shown."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from rich.console import Console

from purr.models import PR, FileChange, PRDetails
from purr.tui.overview import (
    DetailsCache,
    details_line,
    render_overview,
    short_age,
    where_it_changes,
)

PR1 = PR(
    repo="a/b",
    number=1,
    title="Fix",
    author="alice",
    url="u",
    head_sha="abc",
    updated_at="2026-11-20T10:00:00Z",
    assignees=("bob",),
)
DETAILS = PRDetails(body="Hi", base="main", head="fix", additions=3, deletions=1, changed_files=1)


def test_the_cache_fetches_each_pr_once_until_it_changes():
    cache = DetailsCache()
    assert cache.claim([PR1]) == [PR1]
    assert cache.claim([PR1]) == []  # already on its way
    assert cache.put(PR1, DETAILS)
    assert cache.get(PR1) == DETAILS
    assert cache.claim([PR1]) == []  # fresh

    # An update on GitHub makes it stale; same details again isn't a change.
    updated = replace(PR1, updated_at="2026-11-21T10:00:00Z")
    assert cache.claim([updated]) == [updated]
    assert not cache.put(updated, DETAILS)

    # A failed fetch lets it be claimed again.
    cache.claim([replace(PR1, number=2)])
    cache.put(replace(PR1, number=2), None)
    assert cache.claim([replace(PR1, number=2)]) == [replace(PR1, number=2)]


def test_short_age():
    now = datetime.now(UTC)
    assert short_age((now - timedelta(seconds=5)).isoformat()) == "now"
    assert short_age((now - timedelta(hours=3, minutes=1)).isoformat()) == "3h"
    assert short_age((now - timedelta(days=9)).isoformat()) == "1w"
    assert short_age((now - timedelta(days=400)).isoformat()) == "1y"
    assert short_age(None) == ""


def test_details_line_and_overview():
    line = details_line(PR1, replace(DETAILS, checks="FAILURE", labels=("bug",))).plain
    assert line == "by alice · bob on it · main ← fix · +3 −1 in 1 file · ✗ checks fail · bug"
    assert details_line(PR1, None).plain == "by alice · bob on it"

    # Narrower, it wraps between its parts, never through one: where it stands (checks and
    # reviews) goes to the next line whole.
    standing = replace(DETAILS, checks="SUCCESS", review_decision="APPROVED")
    console = Console(width=58, record=True, file=io.StringIO())
    console.print(details_line(PR1, standing))
    assert console.export_text().splitlines() == [
        "by alice · bob on it · main ← fix · +3 −1 in 1 file",
        "✓ checks pass · approved",
    ]

    files = (FileChange("src/bus.py", 3, 1),)
    overview = plain(render_overview(replace(DETAILS, files=files)))
    assert "1 file changed: src/bus.py  · d to read them and comment" in overview
    assert "Loading" in str(render_overview(None))
    assert "boom" in str(render_overview(None, "boom"))


def test_where_a_change_is():
    def where(*files: tuple[str, int]) -> str:
        changes = tuple(FileChange(path, lines, 0) for path, lines in files)
        return where_it_changes(replace(DETAILS, files=changes))

    assert where(("src/bus.py", 3), ("README.md", 1)) == "src/bus.py, README.md"
    # More: the folders with most lines changed, without what they all share.
    assert (
        where(
            ("src/pipecat/rtvi/a.py", 100),
            ("src/pipecat/rtvi/b.py", 50),
            ("src/pipecat/evals/c.py", 30),
            ("src/pipecat/d.py", 10),
            ("src/pipecat/flows/e.py", 1),
        )
        == "most in rtvi (2), evals (1), src/pipecat (1)"
    )


def plain(renderable) -> str:
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_diffs_are_cached_by_head():
    cache = DetailsCache()
    assert cache.claim_diffs([PR1]) == [PR1]
    assert cache.claim_diffs([PR1]) == []  # on its way
    assert cache.put_diff(PR1, "diff") and cache.diff(PR1) == "diff"
    assert cache.claim_diffs([PR1]) == []
    moved = replace(PR1, head_sha="new")
    assert cache.diff(moved) is None and cache.claim_diffs([moved]) == [moved]
    assert not cache.put_diff(moved, None)  # failed: try again next time
    assert cache.claim_diffs([moved]) == [moved]
