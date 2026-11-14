from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """String key/value store where every entry expires.

    Implementations must behave identically (see the shared contract tests):
    ``get`` never extends the TTL, and ``pop`` returns the value at most once
    even under concurrent callers.
    """

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int) -> None:
        """Store ``value`` for ``ttl`` seconds (``ttl`` must be positive)."""
        ...

    async def pop(self, key: str) -> str | None:
        """Atomically read and delete; for one-time payloads."""
        ...

    async def delete(self, key: str) -> None: ...

    async def ping(self) -> bool:
        """Connectivity check for readiness probes (raises when unreachable)."""
        ...

    async def aclose(self) -> None: ...


def check_ttl(ttl: int) -> None:
    if ttl <= 0:
        raise ValueError("ttl 必须为正整数（秒）")
