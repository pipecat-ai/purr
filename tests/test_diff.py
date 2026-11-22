"""Reading a PR's diff: files, numbered lines, and where a comment on each goes."""

from purr.diff import parse

DIFF = """\
diff --git a/src/bus.py b/src/bus.py
index 1111111..2222222 100644
--- a/src/bus.py
+++ b/src/bus.py
@@ -10,4 +10,5 @@ class Bus:
     def send(self):
-        self.queue.put(x)
+        if self.running:
+            self.queue.put(x)
--- an SQL comment, deleted
 
diff --git a/old.txt b/new.txt
similarity index 90%
rename from old.txt
rename to new.txt
diff --git a/poetry.lock b/poetry.lock
new file mode 100644
--- /dev/null
+++ b/poetry.lock
@@ -0,0 +1 @@
+x = 1
\\ No newline at end of file
"""


def test_files_and_their_lines_numbered():
    bus, renamed, lock = parse(DIFF)
    assert (bus.path, bus.additions, bus.deletions, bus.note) == ("src/bus.py", 2, 2, "")
    assert [(line.kind, line.old, line.new) for line in bus.lines] == [
        ("@@", None, None),
        (" ", 10, 10),
        ("-", 11, None),
        ("+", None, 11),
        ("+", None, 12),
        ("-", 12, None),  # "--- ..." in a hunk is a deleted line, not a header
        (" ", 13, 13),  # an empty line is context
    ]
    assert (renamed.path, renamed.note, renamed.lines) == ("new.txt", "renamed from old.txt", ())
    assert (lock.note, lock.generated, bus.generated) == ("new file", True, False)
    assert lock.lines[-1].kind == "\\"


def test_where_a_comment_goes():
    bus = parse(DIFF)[0]
    targets = [line.target() for line in bus.lines]
    assert targets == [None, (10, "RIGHT"), (11, "LEFT"), (11, "RIGHT"), (12, "RIGHT"),
                       (12, "LEFT"), (13, "RIGHT")]  # fmt: skip
    assert bus.at(12, "RIGHT") == 4 and bus.at(12, "LEFT") == 5 and bus.at(99, "RIGHT") is None
