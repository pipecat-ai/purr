"""A fixed number of slots, handed to whoever waits by priority, then in order of arrival."""

import asyncio
import heapq
import itertools
from collections.abc import Hashable


class Slots:
    """``size`` slots. Waiting is in line by rank (lower first), then arrival.

    ``join`` gets in line and returns a future that's done once a slot is
    yours; ``leave`` steps out of line (or gives back a slot handed over just as
    you stopped waiting); ``release`` gives a held slot to the next in line.
    """

    def __init__(self, size: int):
        self._free = size
        self._line: list[tuple[int, int, asyncio.Future[None], Hashable]] = []
        self._arrivals = itertools.count()

    def waiting(self) -> list[Hashable]:
        """Who's waiting, first in line first."""
        return [key for _, _, future, key in sorted(self._line) if not future.done()]

    def join(self, key: Hashable, rank: int = 0) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if self._free and not self._line:
            self._free -= 1
            future.set_result(None)
        else:
            heapq.heappush(self._line, (rank, next(self._arrivals), future, key))
        return future

    def leave(self, future: asyncio.Future[None]) -> None:
        if future.done() and not future.cancelled():
            self.release()  # it was ours already
            return
        future.cancel()
        self._line = [entry for entry in self._line if entry[2] is not future]
        heapq.heapify(self._line)

    def release(self) -> None:
        while self._line:
            _, _, future, _ = heapq.heappop(self._line)
            if not future.done():
                future.set_result(None)
                return
        self._free += 1
