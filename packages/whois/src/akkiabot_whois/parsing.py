"""Turn user input into IP addresses."""

from __future__ import annotations

import re
from ipaddress import ip_address

from akkiabot_whois.errors import InvalidIpError
from akkiabot_whois.models import IpAddress

# Runs of characters that can make up an IPv4 / IPv6 address (optionally with a port).
_CANDIDATE = re.compile(r"[0-9A-Fa-f:.]{2,}")


def parse_ip(text: str) -> IpAddress:
    """Parse one address; raises :class:`InvalidIpError` when invalid."""
    candidate = text.strip().removeprefix("[").removesuffix("]")
    try:
        return ip_address(candidate)
    except ValueError:
        raise InvalidIpError(f"不是合法的 IP 地址：{text!r}") from None


def ensure_public(ip: IpAddress) -> IpAddress:
    """Reject private, loopback, reserved… addresses: no provider can answer them."""
    if not ip.is_global or ip.is_multicast:
        raise InvalidIpError(f"{ip} 不是公网地址")
    return ip


def extract_ip(text: str) -> IpAddress | None:
    """The first IP address found anywhere in ``text`` (e.g. a replied message)."""
    for match in _CANDIDATE.finditer(text):
        token = match.group().strip(".")
        candidates = [token]
        if token.count(":") == 1 and "." in token:  # 1.2.3.4:443
            candidates.append(token.split(":", 1)[0])
        for candidate in candidates:
            try:
                return ip_address(candidate)
            except ValueError:
                continue
    return None
