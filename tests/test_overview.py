"""PR details: the cache in front of GitHub, and how they're shown."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from rich.console import Console

from purr.models import PR, PRDetails
from purr.tui.overview import (
    DetailsCache,
    details_line,
    file_path,
    opens_short,
    render_diff,
    render_overview,
    short_age,
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
    assert line == "by alice · bob on it · main ← fix · +3 −1 · ✗ checks fail · bug"
    assert details_line(PR1, None).plain == "by alice · bob on it"

    overview = render_overview(replace(DETAILS, changed_files=45))
    texts = [str(part) for part in overview.renderables if hasattr(part, "plain")]
    assert (
        "45 files changed  +3 −1  click a file to open it · e opens all · d to read and comment"
        in texts
    )
    assert "Loading the diff…" in texts
    failed = render_overview(DETAILS, diff_error="too big")
    assert "Couldn't load the diff: too big" in plain(failed)
    assert "Loading" in str(render_overview(None))
    assert "boom" in str(render_overview(None, "boom"))


DIFF = """diff --git a/src/bus.py b/src/bus.py
index 1111111..2222222 100644
--- a/src/bus.py
+++ b/src/bus.py
@@ -1,3 +1,3 @@ class Bus:
 def subscribe(self):
-    pass
+    self._subscribers.append(x)
diff --git a/uv.lock b/uv.lock
index 3333333..4444444 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1 +1 @@
-old = 1
+new = 2
diff --git a/docs/new.md b/docs/new.md
new file mode 100644
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1 @@
+Hello
"""


def plain(renderable) -> str:
    console = Console(width=80, color_system=None)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def test_the_diff_is_its_files_a_line_each_until_opened():
    closed = plain(render_diff(DIFF))
    assert closed.splitlines() == [
        "▸ src/bus.py  +1 −1",
        "▸ uv.lock  +1 −1",
        "▸ docs/new.md  +1 −0  new file",
    ]
    assert file_path("▸ src/bus.py  +1 −1") == "src/bus.py" and file_path("+ x") is None

    text = plain(render_diff(DIFF, {"src/bus.py", "uv.lock", "docs/new.md"}))
    assert "▾ src/bus.py  +1 −1\n@@ -1,3 +1,3 @@ class Bus:" in text
    assert "-    pass\n+    self._subscribers.append(x)" in text
    assert "index 1111111" not in text and "+++ b/src/bus.py" not in text
    # Lock files stay shut; new files say so.
    assert "uv.lock" in text and "generated, not shown" in text and "new = 2" not in text
    assert "+Hello" in text


def test_a_short_diff_starts_opened():
    assert opens_short(DIFF) == {"src/bus.py", "uv.lock", "docs/new.md"}
    assert opens_short(DIFF * 10) == set() and opens_short(None) == set()


def test_long_diffs_are_cut(monkeypatch):
    monkeypatch.setattr("purr.tui.overview.DIFF_LINES", 5)
    text = plain(render_diff(DIFF, {"src/bus.py", "docs/new.md"}))
    assert "+    self._subscribers" in text  # 4 lines: fits in 5
    assert "too long to show here: d opens it" in text and "+Hello" not in text


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
