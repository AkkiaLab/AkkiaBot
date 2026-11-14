"""Provider chain with fallback and a result cache."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from akkiabot_whois.cache import TTLCache
from akkiabot_whois.errors import ProviderError
from akkiabot_whois.models import IpAddress, IpInfo
from akkiabot_whois.parsing import ensure_public, parse_ip
from akkiabot_whois.providers import IpProvider

logger = logging.getLogger(__name__)

type LookupObserver = Callable[[str, str], None]
"""Called as ``observer(provider, result)`` for every outcome (metrics hook).

``result`` is one of :data:`RESULTS`. A cache hit reports provider
``"cache"``; a lookup where every provider failed reports provider ``"none"``
with result ``"exhausted"``.
"""

RESULTS = ("ok", "error", "hit", "exhausted")


class WhoisService:
    """Ask ``providers`` in order; the first answer wins and is cached.

    Failures are not cached, so the next lookup tries again.
    """

    def __init__(
        self,
        providers: Sequence[IpProvider],
        cache: TTLCache[str, IpInfo] | None = None,
        *,
        observer: LookupObserver | None = None,
    ) -> None:
        if not providers:
            raise ValueError("至少需要一个 provider")
        self.providers = tuple(providers)
        self.cache = cache
        self._observer = observer

    def _observe(self, provider: str, result: str) -> None:
        if self._observer is not None:
            self._observer(provider, result)

    async def lookup(self, ip: IpAddress | str) -> IpInfo | None:
        """Look a public address up; ``None`` when every provider failed.

        Raises :class:`InvalidIpError` for malformed or non-public input,
        before any network access.
        """
        address = ensure_public(parse_ip(ip) if isinstance(ip, str) else ip)
        key = str(address)
        if self.cache is not None and (cached := self.cache.get(key)) is not None:
            self._observe("cache", "hit")
            return cached

        for provider in self.providers:
            try:
                info = await provider.lookup(address)
            except ProviderError as exc:
                self._observe(provider.name, "error")
                logger.warning(
                    "whois provider failed",
                    extra={"provider": provider.name, "ip": key, "reason": exc.reason},
                )
                continue
            self._observe(provider.name, "ok")
            if self.cache is not None:
                self.cache.set(key, info)
            return info

        self._observe("none", "exhausted")
        logger.error("whois lookup exhausted all providers", extra={"ip": key})
        return None
