"""In-memory sliding-window rate limit per key (PLAN §10; DECISIONS 1b item 9).

State lives in one API process: with several uvicorn workers each has its own window, so the
effective limit is `limit * workers`. That is accepted: the limit only stops bots.
"""

import threading
import time
from collections import deque
from collections.abc import Callable

WINDOW_S = 3600.0


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = clock()

    def allow(self, key: str) -> bool:
        """Record a hit for `key` and return True, or return False if the window is full."""
        with self._lock:
            now = self._clock()
            if now - self._last_sweep >= self._window_s:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self._window_s:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True

    def _sweep(self, now: float) -> None:
        """Drop keys with no hit inside the window, so memory does not grow with every new IP."""
        cutoff = now - self._window_s
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]
        self._last_sweep = now
