"""What Purr uses from the standard library of Python 3.12, for 3.11."""

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import TypeVar

T = TypeVar("T")


def batched(items: Iterable[T], size: int) -> Iterator[tuple[T, ...]]:
    """``items`` in tuples of ``size`` (the last one shorter): ``itertools.batched``."""
    iterator = iter(items)
    while batch := tuple(islice(iterator, size)):
        yield batch
