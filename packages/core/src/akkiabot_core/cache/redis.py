from __future__ import annotations

from typing import Any, Self

from redis.asyncio import Redis

from akkiabot_core.cache.base import check_ttl


class RedisCache:
    """Redis-backed cache. Requires Redis >= 6.2 (``GETDEL``)."""

    def __init__(self, client: Redis, *, prefix: str = "akkiabot:") -> None:
        self._client = client
        self._prefix = prefix

    @classmethod
    def from_url(cls, url: str, *, prefix: str = "akkiabot:", **kwargs: Any) -> Self:
        return cls(Redis.from_url(url, decode_responses=True, **kwargs), prefix=prefix)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> str | None:
        return _as_str(await self._client.get(self._key(key)))

    async def set(self, key: str, value: str, ttl: int) -> None:
        check_ttl(ttl)
        await self._client.set(self._key(key), value, ex=ttl)

    async def pop(self, key: str) -> str | None:
        return _as_str(await self._client.getdel(self._key(key)))

    async def delete(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def ping(self) -> bool:
        return bool(await self._client.ping())  # pyright: ignore[reportGeneralTypeIssues]

    async def aclose(self) -> None:
        await self._client.aclose()


def _as_str(value: bytes | str | None) -> str | None:
    # Clients created without decode_responses=True return bytes.
    return value.decode() if isinstance(value, bytes) else value
