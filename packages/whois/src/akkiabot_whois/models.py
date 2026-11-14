from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address

from pydantic import BaseModel, ConfigDict

type IpAddress = IPv4Address | IPv6Address


class IpInfo(BaseModel):
    """Normalised lookup result. Fields a provider does not know are ``""``."""

    model_config = ConfigDict(frozen=True)

    ip: str
    location: str
    """``City, Region, Country`` (duplicates and blanks dropped)."""
    asn: str
    """``AS<number> <name>``, e.g. ``AS13335 Cloudflare, Inc.``."""
    org: str
    isp: str
    provider: str
    """Name of the provider that answered."""


def join_location(*parts: str | None) -> str:
    """``City, Region, Country`` without blanks or consecutive duplicates."""
    result: list[str] = []
    for part in parts:
        part = (part or "").strip()
        if part and (not result or result[-1] != part):
            result.append(part)
    return ", ".join(result)
