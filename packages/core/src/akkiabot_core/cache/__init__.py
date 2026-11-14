"""Key/value cache with TTL for short-lived state (start payloads, captcha…)."""

from __future__ import annotations

from akkiabot_core.cache.base import Cache
from akkiabot_core.cache.memory import MemoryCache
from akkiabot_core.cache.redis import RedisCache


def create_cache(redis_url: str | None, *, prefix: str = "akkiabot:") -> Cache:
    """Redis-backed cache when ``redis_url`` is set, otherwise in-process."""
    if redis_url:
        return RedisCache.from_url(redis_url, prefix=prefix)
    return MemoryCache()


__all__ = ["Cache", "MemoryCache", "RedisCache", "create_cache"]
