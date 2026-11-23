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
    text = plain(rows(files, {0}, comments, shut={"github:T2:0"}, width=36))
    assert text[0] == "▾ src/bus.py  +1 −1  ✎ 6"
    assert text[2:] == [  # boxes all the same width, under the code
        "    1    1   def subscribe(self):",
        "            ╭─ ▾ alice ────────────────────────╮",  # a thread: a box a comment
        "            │ Should subscribe take a lock?    │",
        "            ╰──────────────────────────────────╯",
        "            ╭─ ▾ me (you) · reply ─────────────╮",  # a reply: its title says so
        "            │ Done.                            │",
        "            ╰──────────────────────────────────╯",
        "    2       -    pass",
        "            ╭─ ▾ you ──────────────────────────╮",  # on the deleted line: the base's
        "            │ Why drop this?                   │",
        "            ╰──────────────────────────────────╯",
        "         2  +    self._subscribers.append(x)",
        "            ╭─ ▾ agent, proposed ──────────────╮",
        "            │ Race here                        │",
        "            ╰──────────────────────────────────╯",
        "            ╶─ ▸ carol · resolved ─ Typo ─╴",  # shut: its top line, and how it starts
        "            ╭─ ▾ dave · outdated ──────────────╮",  # on no line of this diff: last
        "            │ Old code                         │",
        "            ╰──────────────────────────────────╯",
        "▸ uv.lock  +1 −1",
        "▸ docs/new.md  +1 −0  new file",
    ]


def test_the_conversation_comes_first_its_boxes_not_under_code():
    from purr.models import PRComment
    from purr.tui.diffview import CONVERSATION

    files = (CONVERSATION, *parse(DIFF))
    alice = PRComment("IC1", "comment", "alice", "Can you **rebase**?", "2020-01-01T00:00:00Z")
    bob = PRComment("R1", "review", "bob", "- one\n- two", "2020-01-02T00:00:00Z",
                    state="APPROVED")  # fmt: skip
    text = plain(rows(files, {0}, said([], [], [], conversation=(alice, bob)), width=36))
    assert text[:8] == [  # boxes as on lines, the same width, but not indented
        "▾ Conversation  on the PR  · c writes on it  ✎ 2",
        "╭─ ▾ alice · 6y ago ───────────────╮",
        "│ Can you rebase?                  │",  # its words as Markdown
        "╰──────────────────────────────────╯",
        "╭─ ▾ bob · 6y ago · approved ──────╮",  # a review's text, with its verdict
        "│  • one                           │",
        "│  • two                           │",
        "╰──────────────────────────────────╯",
    ]
    assert text[8] == "▸ src/bus.py  +1 −1"


def test_a_reply_starts_with_what_it_answers_quoted():
    from purr.tui.diffview import quoted

    assert quoted("Should it?\n\nI think so.") == "> Should it?\n>\n> I think so.\n\n"


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


def test_code_in_its_languages_colors_and_changed_lines_on_green_or_red():
    from rich.style import Style

    from purr.tui.code import highlighted
    from purr.tui.diffview import _changed
    from purr.tui.theme import palette

    [file] = parse(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n"
        ' def f():\n-    """Old\n-    doc."""\n+    """New\n+    doc."""\n'
    )
    colored = highlighted(file, "solarized-dark")
    assert colored[0] is None  # the hunk's header isn't code
    assert [text.plain for text in colored[1:]] == [
        "def f():",
        '    """Old',
        '    doc."""',
        '    """New',
        '    doc."""',
    ]
    # A docstring over two lines is colored as one, on each side; and with no background
    # of its own (the diff's goes behind it).
    styles = [{str(span.style.color) for span in text.spans} for text in colored[2:]]
    assert len({frozenset(s) for s in styles if s}) <= 2
    assert all(span.style.bgcolor is None for text in colored[1:] for span in text.spans)

    view = rows([file], {0}, [])
    lines = {row.line.kind: row for row in view if row.kind == "line"}
    assert view[2].text.plain == "    1    1   def f():"
    assert _changed(lines["+"]) == Style(bgcolor=palette().added)
    assert _changed(lines["-"]) == Style(bgcolor=palette().removed)
    assert _changed(lines[" "]) == Style() == _changed(view[0])


def test_code_in_no_known_language_is_plain():
    from purr.tui.code import highlighted

    [file] = parse("diff --git a/x.zzz b/x.zzz\n--- a/x.zzz\n+++ b/x.zzz\n@@ -1 +1 @@\n-a\n+b\n")
    assert highlighted(file, "solarized-dark") == (None, None, None)
    assert [row.text.plain for row in rows([file], {0}, [])][1:] == [
        "            @@ -1 +1 @@",
        "    1       -a",
        "         1  +b",
    ]
