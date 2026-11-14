"""In-process sliding-window rate limiter.

The bot runs as a single process (docs/architecture.md §5.4), so per-user
limits do not need a shared store.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable


class RateLimiter:
    """Allow at most ``limit`` hits per key within any ``window`` seconds."""

    def __init__(
        self, limit: int, window: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if limit <= 0 or window <= 0:
            raise ValueError("limit 与 window 必须为正数")
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}
        self._last_sweep = clock()

    def acquire(self, key: Hashable) -> float:
        """Record a hit for ``key``. Returns ``0`` when allowed, otherwise the
        seconds to wait before the next hit would be allowed (nothing recorded)."""
        now = self._clock()
        self._sweep(now)
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window - now
        hits.append(now)
        return 0.0

    def _sweep(self, now: float) -> None:
        """Forget idle keys once per window so memory stays bounded."""
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    def __len__(self) -> int:
        return len(self._hits)
