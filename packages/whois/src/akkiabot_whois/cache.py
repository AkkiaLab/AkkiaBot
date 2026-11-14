"""A small in-process TTL cache for lookup results."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class TTLCache[K, V]:
    """Bounded LRU mapping whose entries expire ``ttl`` seconds after insertion."""

    def __init__(
        self,
        *,
        ttl: float,
        maxsize: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl <= 0:
            raise ValueError("ttl 必须为正数（秒）")
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正整数")
        self.ttl = ttl
        self.maxsize = maxsize
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        self._data[key] = (self._clock() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
