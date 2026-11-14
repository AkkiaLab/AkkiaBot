from __future__ import annotations

import time
from collections.abc import Callable

from akkiabot_core.cache.base import check_ttl


class MemoryCache:
    """In-process cache for local development, tests and single-instance runs.

    Expired entries are dropped lazily on access and swept when the store grows.
    Operations never await, so they are atomic within one event loop.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_entries: int = 10_000):
        self._clock = clock
        self._max_entries = max_entries
        self._data: dict[str, tuple[str, float]] = {}

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at <= self._clock():
            del self._data[key]
            return None
        return value

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, (_, exp) in self._data.items() if exp <= now]:
            del self._data[key]

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        check_ttl(ttl)
        if len(self._data) >= self._max_entries:
            self._sweep()
        self._data[key] = (value, self._clock() + ttl)

    async def pop(self, key: str) -> str | None:
        value = self._live(key)
        if value is not None:
            del self._data[key]
        return value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def aclose(self) -> None:
        self._data.clear()
