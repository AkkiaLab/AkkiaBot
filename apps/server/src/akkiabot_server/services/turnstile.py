"""Cloudflare Turnstile server-side validation (``siteverify``).

See https://developers.cloudflare.com/turnstile/get-started/server-side-validation/.
A token is single use and valid for 300 seconds; Cloudflare answers
``success: false`` with ``error-codes`` for invalid, expired or reused tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnstileResult:
    success: bool
    error_codes: tuple[str, ...] = field(default_factory=tuple)


class TurnstileVerifier(Protocol):
    async def verify(self, token: str) -> TurnstileResult: ...


class _SiteverifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    success: bool
    error_codes: list[str] = Field(default_factory=list, alias="error-codes")


class HttpTurnstileVerifier:
    """Calls ``siteverify`` with the shared async HTTP client.

    Network errors and malformed answers are reported as a failed validation
    (``request_failed`` / ``invalid_response``), never raised. The secret is
    only sent in the form body and never logged.
    """

    def __init__(
        self, http: httpx.AsyncClient, *, secret_key: str, verify_url: str, timeout: float
    ) -> None:
        self._http = http
        self._secret_key = secret_key
        self._verify_url = verify_url
        self._timeout = timeout

    async def verify(self, token: str) -> TurnstileResult:
        try:
            response = await self._http.post(
                self._verify_url,
                data={"secret": self._secret_key, "response": token},
                timeout=self._timeout,
            )
            response.raise_for_status()
            parsed = _SiteverifyResponse.model_validate_json(response.content)
        except httpx.HTTPError as exc:
            logger.warning(
                "turnstile siteverify request failed", extra={"error": type(exc).__name__}
            )
            return TurnstileResult(False, ("request_failed",))
        except ValidationError:
            logger.warning("turnstile siteverify returned an invalid response")
            return TurnstileResult(False, ("invalid_response",))
        return TurnstileResult(parsed.success, tuple(parsed.error_codes))
