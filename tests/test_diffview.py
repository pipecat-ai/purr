"""The diff view's lines: files, numbered lines, and comments under the lines they're on."""

from purr.diff import parse
from purr.models import DraftComment
from purr.tui.diffview import first_opened, opens_short, rows, said

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


def plain(view_rows) -> list[str]:
    return [row.text.plain for row in view_rows]


def test_files_start_as_a_line_each():
    files = parse(DIFF)
    assert plain(rows(files, set(), [])) == [
        "▸ src/bus.py  +1 −1",
        "▸ uv.lock  +1 −1",
        "▸ docs/new.md  +1 −0  new file",
    ]
    opened = plain(rows(files, {0, 1}, []))
    assert opened[:5] == [
        "▾ src/bus.py  +1 −1",
        "            @@ -1,3 +1,3 @@ class Bus:",
        "    1    1   def subscribe(self):",
        "    2       -    pass",
        "         2  +    self._subscribers.append(x)",
    ]
    assert "  generated, not shown" in opened  # a lock file stays shut


def test_comments_under_their_lines():
    files = parse(DIFF)
    mine = [
        DraftComment(1, "src/bus.py", 2, "LEFT", "Why drop this?", "abc"),
        DraftComment(2, "src/bus.py", 9, "RIGHT", "Way down", "abc"),
    ]
    agent = [{"path": "src/bus.py", "line": 2, "body": "Race here"}, {"path": "x.py"}]
    comments = said(mine, agent)
    assert len(comments) == 3  # one without a line can't be shown
    text = plain(rows(files, {0}, comments))
    assert text[0] == "▾ src/bus.py  +1 −1  ✎ 3"
    assert text[3:7] == [
        "    2       -    pass",
        "            ✎ you: Why drop this?",
        "         2  +    self._subscribers.append(x)",
        "            ✎ agent: Race here",
    ]
    assert text[7] == "            ✎ you (line 9, not in the diff): Way down"


def test_which_files_start_opened():
    files = parse(DIFF)
    assert opens_short(DIFF) == {"src/bus.py", "uv.lock", "docs/new.md"}
    assert opens_short(DIFF * 10) == set() and opens_short(None) == set()
    assert first_opened(files, DIFF, []) == {0, 1, 2}  # short: all
    commented = said([], [{"path": "docs/new.md", "line": 1, "body": "Hi"}])
    assert first_opened(files, DIFF * 10, commented) == {2}  # long: those with comments
