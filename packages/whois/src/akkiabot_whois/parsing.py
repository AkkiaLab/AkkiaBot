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
        for candidate in _candidates(match.group().strip(".")):
            try:
                return ip_address(candidate)
            except ValueError:
                continue
    return None


def _candidates(token: str) -> list[str]:
    """Interpretations of one character run, most literal first.

    A label colon (``IP:1.1.1.1``, ``ip:2001:db8::1``) or a port
    (``1.1.1.1:443``) is glued to the address by the character class, so also
    try the run without leading / trailing colons and, for IPv4, each
    colon-separated part.
    """
    candidates = [token]
    trimmed = token.strip(":")
    if trimmed and trimmed != token:
        candidates.append(trimmed)
    if "." in token:
        candidates.extend(part.strip(".") for part in token.split(":") if "." in part)
    return candidates
