"""Pacing for automatic downloads, so one refresh cannot start a flood.

A refresh batch can import new episodes for hundreds of shows at once, and
each show's own limit (1-10 per refresh) multiplies across them: a week away
from a large library could queue thousands of transfers against the same few
podcast CDNs. Candidates wait here instead and are released round-robin
across shows (newest first within a show) under one rolling hourly budget
shared by every show. The queue is plain data owned by the GUI thread; it
starts nothing itself.
"""

import time
from collections import OrderedDict, deque

WINDOW_SECONDS = 3600.0
DEFAULT_HOURLY_LIMIT = 20
HOURLY_LIMIT_RANGE = (1, 200)


class AutoDownloadQueue:
    def __init__(self, hourly_limit: int = DEFAULT_HOURLY_LIMIT, clock=time.monotonic):
        self.hourly_limit = hourly_limit
        self._clock = clock
        self._pending = OrderedDict()  # show id -> deque of episode ids, newest first
        self._started = deque()  # clock stamps of the releases still inside the window

    def __len__(self):
        return sum(len(queued) for queued in self._pending.values())

    def add(self, show_id: int, episode_ids) -> int:
        """Queue a show's candidates behind the ones already waiting; returns how many were new."""
        queued = self._pending.get(show_id)
        known = set(queued or ())
        added = [episode_id for episode_id in dict.fromkeys(episode_ids) if episode_id not in known]
        if added:
            if queued is None:
                queued = self._pending[show_id] = deque()
            queued.extend(added)
        return len(added)

    def discard_show(self, show_id: int) -> None:
        self._pending.pop(show_id, None)

    def available(self) -> int:
        """Releases the budget still allows in the current window."""
        cutoff = self._clock() - WINDOW_SECONDS
        while self._started and self._started[0] <= cutoff:
            self._started.popleft()
        return max(0, int(self.hourly_limit) - len(self._started))

    def take(self) -> list[int]:
        """Episode ids allowed to start now, one show at a time in turn, each
        counted against the budget. A show with a backlog cannot starve the
        shows queued after it."""
        budget, released = self.available(), []
        while budget > 0 and self._pending:
            show_id, queued = next(iter(self._pending.items()))
            released.append(queued.popleft())
            budget -= 1
            if queued:
                self._pending.move_to_end(show_id)
            else:
                del self._pending[show_id]
        now = self._clock()
        self._started.extend(now for _ in released)
        return released

    def refund(self, count: int) -> None:
        """Give back releases that turned out to start nothing (the episode was
        played, deleted or downloaded by hand while it waited)."""
        for _ in range(min(max(0, count), len(self._started))):
            self._started.pop()

    def seconds_until_release(self) -> float | None:
        """When waiting candidates can next be released; None when none wait."""
        if not self._pending:
            return None
        if self.available():
            return 0.0
        return max(0.0, self._started[0] + WINDOW_SECONDS - self._clock())
