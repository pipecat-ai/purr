"""A PR's unified diff, read: its files, their lines, and where a comment on each goes.

GitHub places an inline comment by file, line and side: a line the PR adds or
keeps is on the ``RIGHT`` (its number in the PR's version), a line it deletes on
the ``LEFT`` (its number in the base). Only lines in the diff's hunks take one.

What the diff leaves out of a file, around its hunks, are its gaps (:func:`gaps`):
lines the PR doesn't change, which can be shown from the file as it is in the PR.

Commits on top of the PR move its lines: :func:`mover` says where one is now.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import lru_cache
from itertools import pairwise

# Generated files whose diff is noise: just their name and size.
GENERATED = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum", "*.min.js", "*.svg")

_MARKS = {"+", "-", "\\"}  # how a hunk line starts, besides a space
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SPAN = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@")  # with their lengths

# What git says about a file before its hunks, as a short note.
_NOTES = {
    "new file mode": "new file",
    "deleted file mode": "deleted",
    "rename from ": "renamed from ",
    "Binary files": "binary",
}


@dataclass(frozen=True)
class Line:
    """A line of a file's diff: ``kind`` is how it starts (``+``, ``-``, `` ``, ``@@``,
    or ``\\`` for git's "no newline" note); ``old``/``new`` are its numbers."""

    kind: str
    text: str  # as in the diff, its +/-/space included
    old: int | None = None
    new: int | None = None
    outside: bool = False  # from a gap, shown around the hunks: not one to comment on

    def target(self) -> tuple[int, str] | None:
        """Where a comment on it goes: (line, side); None if it can't take one."""
        if self.kind == "-" and self.old:
            return self.old, "LEFT"
        if self.kind in ("+", " ") and self.new:
            return self.new, "RIGHT"
        return None


@dataclass(frozen=True)
class File:
    path: str
    lines: tuple[Line, ...]
    note: str = ""  # "new file", "deleted", "renamed from x", "binary"

    @property
    def additions(self) -> int:
        return sum(line.kind == "+" for line in self.lines)

    @property
    def deletions(self) -> int:
        return sum(line.kind == "-" for line in self.lines)

    @property
    def generated(self) -> bool:
        name = self.path.rpartition("/")[2]
        return any(fnmatch(name, pattern) for pattern in GENERATED)

    def at(self, line: int, side: str) -> int | None:
        """The index of the line a comment at (line, side) is on, if it's in the diff."""
        return next((i for i, x in enumerate(self.lines) if x.target() == (line, side)), None)


@dataclass(frozen=True)
class Gap:
    """Lines a file's diff leaves out: before the hunk whose header is ``file.lines[at]``,
    or after the last hunk (``at`` is ``len(file.lines)``). From ``first`` to ``last`` in
    the PR's version (``last`` None: to the end of the file, which the diff doesn't say);
    in the base, each is ``offset`` lines earlier."""

    at: int
    first: int
    last: int | None
    offset: int

    def lines(self, source: list[str]) -> list[Line]:
        """Its lines, from the file as it is in the PR (``source``, a line each)."""
        last = len(source) if self.last is None else min(self.last, len(source))
        return [
            Line(" ", f" {source[new - 1]}", new - self.offset, new, outside=True)
            for new in range(self.first, last + 1)
        ]


@lru_cache(maxsize=64)
def gaps(file: File) -> tuple[Gap, ...]:
    """What the file's diff leaves out, around its hunks. Nothing for a file the PR adds or
    deletes (all of it is in the diff), or one with no hunks (binary, only renamed)."""
    if not file.lines or file.note.startswith(("new file", "deleted", "binary")):
        return ()
    found: list[Gap] = []
    old = new = 0  # the last line numbers seen, in the base and the PR
    for index, line in enumerate(file.lines):
        if line.kind == "@@" and (hunk := _HUNK.match(line.text)):
            old_start, new_start = int(hunk[1]), int(hunk[2])
            if new_start > new + 1:
                found.append(Gap(index, new + 1, new_start - 1, new_start - old_start))
        old, new = line.old or old, line.new or new
    if file.lines[-1].kind != "\\":  # git says when the file ends without a newline
        found.append(Gap(len(file.lines), new + 1, None, new - old))
    return tuple(found)


@lru_cache(maxsize=16)  # a PR's pane redraws often, from the same diff
def parse(diff: str) -> tuple[File, ...]:
    """The files in a ``git diff``, in order."""
    lines = diff.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]
    return tuple(_file(lines[start:end]) for start, end in pairwise([*starts, len(lines)]))


def _file(lines: list[str]) -> File:
    """A file from its part of the diff: the ``diff --git`` line, the header, the hunks."""
    path = lines[0].rpartition(" b/")[2]
    first_hunk = next((i for i, line in enumerate(lines) if line.startswith("@@")), len(lines))
    note = " · ".join(filter(None, map(_note, lines[1:first_hunk])))
    return File(path, tuple(_hunks(lines[first_hunk:])), note)


def _note(line: str) -> str:
    for prefix, note in _NOTES.items():
        if line.startswith(prefix):
            return note + line.removeprefix(prefix) if note.endswith(" ") else note
    return ""


def _hunks(lines: list[str]) -> list[Line]:
    """Hunk lines, numbered: a context line is in both versions, an added one in the new,
    a deleted one in the old."""
    parsed, old, new = [], 0, 0
    for text in lines:
        if hunk := _HUNK.match(text):
            old, new = int(hunk[1]), int(hunk[2])
            parsed.append(Line("@@", text))
            continue
        kind = text[:1] if text[:1] in _MARKS else " "  # an empty line: context
        in_old, in_new = kind in ("-", " "), kind in ("+", " ")
        parsed.append(Line(kind, text, old if in_old else None, new if in_new else None))
        old += in_old
        new += in_new
    return parsed


def mover(changes: str) -> Callable[[str, int], int]:
    """Where a line of a file is after ``changes`` (a ``git diff -U0`` of it), by its
    number before them: 0 if they changed that line, or deleted the file."""
    spans: dict[str, list[tuple[int, int, int]]] = {}  # path -> (first, length, shift)
    gone: set[str] = set()
    path = ""
    for text in changes.splitlines():
        if text.startswith("diff --git "):
            path = text.rpartition(" b/")[2]
        elif text.startswith("deleted file mode"):
            gone.add(path)
        elif span := _SPAN.match(text):  # git leaves out a length of 1
            first, length, now = int(span[1]), int(span[2] or 1), int(span[3] or 1)
            spans.setdefault(path, []).append((first, length, now - length))

    def moved(path: str, line: int) -> int:
        if path in gone:
            return 0
        shift = 0
        for first, length, more in spans.get(path, ()):
            if line < first + (length == 0):  # lines added go after line ``first``
                break
            if line < first + length:
                return 0
            shift += more
        return line + shift

    return moved
