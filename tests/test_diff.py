"""Reading a PR's diff: files, numbered lines, and where a comment on each goes."""

from purr.diff import mover, parse

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


GAPPED = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -10,3 +10,4 @@ def f():
 a
-b
+B
+C
 c
@@ -30,2 +31,2 @@ def g():
 x
-y
+Y
"""


def test_what_the_diff_leaves_out_around_its_hunks():
    from purr.diff import Gap, gaps

    [file] = parse(GAPPED)
    # Before the first hunk, between the hunks, and after the last (to the end, which
    # the diff doesn't say); numbered in the PR, each so many lines later than the base.
    assert gaps(file) == (
        Gap(at=0, first=1, last=9, offset=0),
        Gap(at=6, first=14, last=30, offset=1),
        Gap(at=10, first=33, last=None, offset=1),
    )
    source = [f"line {n}" for n in range(1, 36)]
    between = gaps(file)[1].lines(source)
    assert (between[0].old, between[0].new, between[0].text) == (13, 14, " line 14")
    assert (between[-1].old, between[-1].new) == (29, 30)
    assert all(line.outside and line.target() == (line.new, "RIGHT") for line in between)
    end = gaps(file)[2].lines(source)
    assert [(line.old, line.new) for line in end] == [(32, 33), (33, 34), (34, 35)]


def test_nothing_left_out_of_new_or_deleted_files_or_after_the_last_line():
    from purr.diff import gaps

    new, deleted = parse(
        "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n"
        "@@ -0,0 +1 @@\n+x\n"
        "diff --git a/d.py b/d.py\ndeleted file mode 100644\n--- a/d.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
    )
    assert gaps(new) == gaps(deleted) == ()
    [ends] = parse(
        "diff --git a/e.py b/e.py\n--- a/e.py\n+++ b/e.py\n@@ -1 +1 @@\n-x\n+y\n"
        "\\ No newline at end of file\n"
    )
    assert gaps(ends) == ()


def test_where_lines_are_after_more_commits():
    changes = """\
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -5,0 +6,2 @@ def f():
+a
+b
@@ -10,2 +11,0 @@
-c
-d
@@ -20 +19,3 @@
-e
+f
+g
+h
diff --git a/gone.py b/gone.py
deleted file mode 100644
"""
    where = mover(changes)
    # Lines added after line 5 push the rest down; deleted or changed ones are gone (0).
    assert [where("x.py", n) for n in (5, 6, 9, 10, 11, 12, 19, 20, 21)] == [
        5, 8, 11, 0, 0, 12, 19, 0, 23,
    ]  # fmt: skip
    assert where("gone.py", 3) == 0
    assert where("same.py", 7) == 7  # a file they didn't touch


def test_paths_git_quotes():
    from purr.diff import unquoted

    # git quotes a path that isn't plain ASCII, with its bytes in octal.
    diff = (
        'diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/docs/caf\\303\\251.md"\n'
        '+++ "b/docs/caf\\303\\251.md"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    [file] = parse(diff)
    assert file.path == "docs/café.md"
    assert mover(diff)("docs/café.md", 1) == 0  # changed
    assert unquoted('"tab\\there \\"quoted\\""') == 'tab\there "quoted"'
    assert unquoted('"café \\"x\\""') == 'café "x"'  # core.quotePath=false: UTF-8 as is
    assert unquoted("plain/path.py") == "plain/path.py"
