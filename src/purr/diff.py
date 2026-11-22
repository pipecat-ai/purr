"""A PR's unified diff, read: its files, their lines, and where a comment on each goes.

GitHub places an inline comment by file, line and side: a line the PR adds or
keeps is on the ``RIGHT`` (its number in the PR's version), a line it deletes on
the ``LEFT`` (its number in the base). Only lines in the diff's hunks take one.
"""

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from functools import lru_cache
from itertools import pairwise

# Generated files whose diff is noise: just their name and size.
GENERATED = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "go.sum", "*.min.js", "*.svg")

_MARKS = {"+", "-", "\\"}  # how a hunk line starts, besides a space
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

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
