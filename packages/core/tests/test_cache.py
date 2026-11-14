"""One contract, every Cache implementation."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import fakeredis
import pytest
from redis.asyncio import Redis

from akkiabot_core.cache import Cache, MemoryCache, RedisCache, create_cache

REDIS_URL = os.environ.get("AKKIABOT_TEST_REDIS_URL")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Backend:
    cache: Cache
    advance: Callable[[float], Awaitable[None]]


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@pytest.fixture(
    params=[
        "memory",
        "fakeredis",
        pytest.param(
            "redis",
            marks=pytest.mark.skipif(REDIS_URL is None, reason="AKKIABOT_TEST_REDIS_URL 未设置"),
        ),
    ]
)
async def backend(request: pytest.FixtureRequest) -> AsyncIterator[Backend]:
    if request.param == "memory":
        clock = FakeClock()

        async def advance(seconds: float) -> None:
            clock.now += seconds

        yield Backend(MemoryCache(clock=clock), advance)
        return

    if request.param == "fakeredis":
        client = fakeredis.FakeAsyncRedis(decode_responses=True)
    else:
        assert REDIS_URL is not None
        client = Redis.from_url(REDIS_URL, decode_responses=True)
        await client.flushdb()
    cache = RedisCache(client, prefix="test:")
    try:
        yield Backend(cache, _sleep)
    finally:
        await cache.aclose()


async def test_is_cache(backend: Backend) -> None:
    assert isinstance(backend.cache, Cache)


async def test_get_missing(backend: Backend) -> None:
    assert await backend.cache.get("nope") is None
    assert await backend.cache.pop("nope") is None


async def test_set_get_overwrite(backend: Backend) -> None:
    await backend.cache.set("k", "v1", ttl=60)
    assert await backend.cache.get("k") == "v1"
    assert await backend.cache.get("k") == "v1"  # get does not consume
    await backend.cache.set("k", "值2", ttl=60)
    assert await backend.cache.get("k") == "值2"


async def test_pop_is_one_shot(backend: Backend) -> None:
    await backend.cache.set("payload", "captcha:-1001234567890", ttl=60)
    assert await backend.cache.pop("payload") == "captcha:-1001234567890"
    assert await backend.cache.pop("payload") is None
    assert await backend.cache.get("payload") is None


async def test_concurrent_pop_returns_value_once(backend: Backend) -> None:
    await backend.cache.set("once", "x", ttl=60)
    results = await asyncio.gather(*(backend.cache.pop("once") for _ in range(10)))
    assert results.count("x") == 1


async def test_delete(backend: Backend) -> None:
    await backend.cache.set("k", "v", ttl=60)
    await backend.cache.delete("k")
    await backend.cache.delete("k")  # idempotent
    assert await backend.cache.get("k") is None


async def test_expires_after_ttl(backend: Backend) -> None:
    await backend.cache.set("short", "v", ttl=1)
    await backend.cache.set("long", "v", ttl=60)
    await backend.advance(1.1)
    assert await backend.cache.get("short") is None
    assert await backend.cache.pop("short") is None
    assert await backend.cache.get("long") == "v"


async def test_get_does_not_refresh_ttl(backend: Backend) -> None:
    await backend.cache.set("k", "v", ttl=2)
    await backend.advance(1.2)
    assert await backend.cache.get("k") == "v"
    await backend.advance(1.2)
    assert await backend.cache.get("k") is None


@pytest.mark.parametrize("ttl", [0, -1])
async def test_non_positive_ttl_rejected(backend: Backend, ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl"):
        await backend.cache.set("k", "v", ttl=ttl)


async def test_memory_cache_sweeps_expired_when_full() -> None:
    clock = FakeClock()
    cache = MemoryCache(clock=clock, max_entries=2)
    await cache.set("a", "1", ttl=1)
    await cache.set("b", "2", ttl=1)
    clock.now += 5
    await cache.set("c", "3", ttl=1)
    assert len(cache._data) == 1  # pyright: ignore[reportPrivateUsage]


async def test_redis_cache_uses_prefix() -> None:
    client = fakeredis.FakeAsyncRedis(decode_responses=True)
    cache = RedisCache(client, prefix="akkiabot:")
    await cache.set("k", "v", ttl=60)
    assert await client.get("akkiabot:k") == "v"
    assert await cache.ping()
    await cache.aclose()


async def test_create_cache_selects_backend() -> None:
    assert isinstance(create_cache(None), MemoryCache)
    redis_cache = create_cache("redis://127.0.0.1:1/0")
    assert isinstance(redis_cache, RedisCache)  # lazy: no connection yet
    await redis_cache.aclose()


async def test_redis_cache_accepts_bytes_client() -> None:
    client = fakeredis.FakeAsyncRedis(decode_responses=False)
    cache = RedisCache(client)
    await cache.set("k", "值", ttl=60)
    assert await cache.get("k") == "值"
    assert await cache.pop("k") == "值"
    await cache.aclose()
