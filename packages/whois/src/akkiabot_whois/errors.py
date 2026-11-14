from __future__ import annotations


class WhoisError(Exception):
    """Base class for whois errors."""


class InvalidIpError(WhoisError, ValueError):
    """The input is not an IP address, or not a public one."""


class ProviderError(WhoisError):
    """A provider could not answer (network error, bad status, bad payload…)."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider}: {reason}")
        self.provider = provider
        self.reason = reason
