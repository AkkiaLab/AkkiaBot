import pytest

from akkiabot_core.cache import MemoryCache
from akkiabot_server.services.start_payload import (
    KEY_PREFIX,
    StartPayload,
    StartPayloadStore,
    start_link,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(clock: Clock) -> MemoryCache:
    return MemoryCache(clock=clock)


async def test_token_is_a_valid_start_parameter(cache: MemoryCache) -> None:
    token = await StartPayloadStore(cache).create("captcha", {"chat_id": -100123})
    assert len(token) <= 64
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


async def test_payload_can_only_be_used_once(cache: MemoryCache) -> None:
    store = StartPayloadStore(cache)
    token = await store.create("captcha", {"chat_id": -100123, "user_id": 1})
    assert await store.consume(token) == StartPayload(
        kind="captcha", data={"chat_id": -100123, "user_id": 1}
    )
    assert await store.consume(token) is None


async def test_tokens_are_unique(cache: MemoryCache) -> None:
    store = StartPayloadStore(cache)
    assert len({await store.create("k") for _ in range(50)}) == 50


async def test_payload_expires_after_default_ttl(cache: MemoryCache, clock: Clock) -> None:
    store = StartPayloadStore(cache, default_ttl=600)
    token = await store.create("captcha")
    clock.now += 599
    assert await cache.get(KEY_PREFIX + token) is not None
    clock.now += 1
    assert await store.consume(token) is None


async def test_payload_custom_ttl(cache: MemoryCache, clock: Clock) -> None:
    store = StartPayloadStore(cache, default_ttl=600)
    token = await store.create("captcha", ttl=30)
    clock.now += 30
    assert await store.consume(token) is None


@pytest.mark.parametrize(
    "token", ["", "short", "has space in it!!", "x" * 65, "../../etc/passwd_x"]
)
async def test_malformed_tokens_never_hit_the_cache(cache: MemoryCache, token: str) -> None:
    if token:
        await cache.set(KEY_PREFIX + token, '{"kind": "x"}', 60)
    assert await StartPayloadStore(cache).consume(token) is None


async def test_corrupt_payload_is_discarded(cache: MemoryCache) -> None:
    token = "A" * 32
    await cache.set(KEY_PREFIX + token, "not json", 60)
    assert await StartPayloadStore(cache).consume(token) is None
    assert await cache.get(KEY_PREFIX + token) is None


def test_start_link() -> None:
    assert start_link("akkiabot", "tok") == "https://t.me/akkiabot?start=tok"
