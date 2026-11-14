"""``/ip`` use case: pick the address, rate-limit per user, look it up."""

from __future__ import annotations

import logging
from collections.abc import Hashable
from dataclasses import dataclass
from enum import StrEnum

import httpx

from akkiabot_core.config import WhoisSettings
from akkiabot_core.observability import count_whois_lookup
from akkiabot_server.services.rate_limit import RateLimiter
from akkiabot_whois import (
    InvalidIpError,
    IpAddress,
    IpApiProvider,
    IpInfo,
    IpinfoProvider,
    IpProvider,
    TTLCache,
    WhoisService,
    ensure_public,
    extract_ip,
    parse_ip,
)

logger = logging.getLogger(__name__)


class IpQueryStatus(StrEnum):
    """Outcome of a query; the value is also the ``command_total`` result label."""

    OK = "ok"
    USAGE = "usage"
    """No argument and no replied message."""
    INVALID_IP = "invalid_ip"
    NO_IP = "no_ip"
    """The replied message contains no IP address."""
    NOT_PUBLIC = "not_public"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"
    """Every provider failed."""


@dataclass(frozen=True, slots=True)
class IpQueryResult:
    status: IpQueryStatus
    ip: IpAddress | None = None
    info: IpInfo | None = None
    retry_after: float = 0.0


class IpLookupService:
    def __init__(self, whois: WhoisService, limiter: RateLimiter) -> None:
        self.whois = whois
        self.limiter = limiter

    async def query(
        self, user: Hashable, argument: str | None, replied_text: str | None
    ) -> IpQueryResult:
        """Look up ``argument``, or the first IP in ``replied_text`` when no
        argument is given. Input errors do not count against the rate limit."""
        if argument:
            try:
                ip = parse_ip(argument)
            except InvalidIpError:
                return IpQueryResult(IpQueryStatus.INVALID_IP)
        elif replied_text:
            ip = extract_ip(replied_text)
            if ip is None:
                return IpQueryResult(IpQueryStatus.NO_IP)
        else:
            return IpQueryResult(IpQueryStatus.USAGE)

        try:
            ensure_public(ip)
        except InvalidIpError:
            return IpQueryResult(IpQueryStatus.NOT_PUBLIC, ip)

        retry_after = self.limiter.acquire(user)
        if retry_after > 0:
            logger.info("ip lookup rate limited", extra={"retry_after": round(retry_after, 1)})
            return IpQueryResult(IpQueryStatus.RATE_LIMITED, ip, retry_after=retry_after)

        info = await self.whois.lookup(ip)
        if info is None:
            return IpQueryResult(IpQueryStatus.FAILED, ip)
        return IpQueryResult(IpQueryStatus.OK, ip, info)


def build_ip_lookup(settings: WhoisSettings, client: httpx.AsyncClient) -> IpLookupService:
    """Provider chain ipinfo → ip-api (ipinfo only when a token is configured)."""
    providers: list[IpProvider] = []
    if settings.ipinfo_token is not None:
        providers.append(IpinfoProvider(client, settings.ipinfo_token.get_secret_value()))
    else:
        logger.warning("WHOIS_IPINFO_TOKEN 未配置，/ip 仅使用 ip-api")
    providers.append(IpApiProvider(client))
    whois = WhoisService(
        providers,
        TTLCache(ttl=settings.cache_ttl, maxsize=settings.cache_size),
        observer=count_whois_lookup,
    )
    return IpLookupService(whois, RateLimiter(settings.rate_limit, settings.rate_window))
