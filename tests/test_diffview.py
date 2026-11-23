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


def test_a_box_for_each_comment_under_its_line():
    files = parse(DIFF)
    mine = [DraftComment(1, "src/bus.py", 2, "LEFT", "Why drop this?", "abc")]
    agent = [{"path": "src/bus.py", "line": 2, "body": "Race here"}, {"path": "x.py"}]
    threads = [
        {"id": "T1", "path": "src/bus.py", "line": 1, "side": "RIGHT", "resolved": False,
         "comments": [{"author": "alice", "body": "Should subscribe take a lock?"},
                      {"author": "me", "by_user": True, "id": "C2", "body": "Done."}]},
        {"id": "T2", "path": "src/bus.py", "line": 2, "side": "RIGHT", "resolved": True,
         "comments": [{"author": "carol", "body": "Typo"}]},
        {"id": "T3", "path": "src/bus.py", "line": None, "resolved": False,
         "comments": [{"author": "dave", "body": "Old code"}]},
    ]  # fmt: skip
    comments = said(mine, agent, threads)
    assert len(comments) == 6  # the agent's without a line can't be shown
    reply = next(c for c in comments if c.id == "C2")
    assert (reply.thread, reply.reply, reply.yours) == ("T1", True, True)
    text = plain(rows(files, {0}, comments, shut={"github:T2:0"}))
    assert text[0] == "▾ src/bus.py  +1 −1  ✎ 6"
    assert text[2:] == [
        "    1    1   def subscribe(self):",
        "            ╭─ ▾ alice ─────────────────────╮",  # a thread: a box a comment
        "            │ Should subscribe take a lock? │",
        "            ╰───────────────────────────────╯",
        "              ╭─ ▾ me (you) · reply ─╮",  # a reply, a little further in
        "              │ Done.                │",
        "              ╰──────────────────────╯",
        "    2       -    pass",
        "            ╭─ ▾ you ────────╮",  # on the deleted line: the base's side
        "            │ Why drop this? │",
        "            ╰────────────────╯",
        "         2  +    self._subscribers.append(x)",
        "            ╭─ ▾ agent, proposed ─╮",
        "            │ Race here           │",
        "            ╰─────────────────────╯",
        "            ╶─ ▸ carol · resolved ─ Typo ─╴",  # shut: its top line, and how it starts
        "            ╭─ ▾ dave · outdated ─╮",  # on no line of this diff: at the end
        "            │ Old code            │",
        "            ╰─────────────────────╯",
        "▸ uv.lock  +1 −1",
        "▸ docs/new.md  +1 −0  new file",
    ]


def test_which_files_start_opened():
    files = parse(DIFF)
    assert opens_short(DIFF) == {"src/bus.py", "uv.lock", "docs/new.md"}
    assert opens_short(DIFF * 10) == set() and opens_short(None) == set()
    assert first_opened(files, DIFF, []) == {0, 1, 2}  # short: all
    commented = said([], [{"path": "docs/new.md", "line": 1, "body": "Hi"}])
    assert first_opened(files, DIFF * 10, commented) == {2}  # long: those with comments


def styles_at(text, start: str) -> set[str]:
    """The styles over where ``start`` is in ``text``."""
    at = text.plain.index(start)
    return {str(span.style) for span in text.spans if span.start <= at < span.end}


def test_boxes_in_color_authors_and_mentions_picked_out():
    files = parse(DIFF)
    threads = [
        {"id": "T1", "path": "src/bus.py", "line": 1, "side": "RIGHT", "resolved": True,
         "comments": [{"author": "alice", "body": "Ask @bob about it"},
                      {"author": "bob", "body": "Done."}]},
    ]  # fmt: skip
    box = rows(files, {0}, said([], [], threads))[3:6]
    top, first = box[0].text, box[1].text
    assert top.plain.startswith("            ╭─ ▾ alice · resolved")
    dated = said(
        [],
        [],
        [{**threads[0], "comments": [{**threads[0]["comments"][0], "at": "2020-01-01T00:00:00Z"}]}],
    )
    assert "alice · 6y ago · resolved" in plain(rows(files, {0}, dated))[3]  # who, and when
    assert "bold cyan" in styles_at(top, "alice · resolved")  # resolved, still in its color
    assert "cyan" in styles_at(top, "╭")
    assert "bold blue" in styles_at(first, "@bob")  # a mention
