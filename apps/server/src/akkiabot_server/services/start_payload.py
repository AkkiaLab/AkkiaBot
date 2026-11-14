"""One-time ``/start`` deep-link payloads.

A feature that needs the user to open a private chat with the bot (captcha,
``/config``…) stores a payload here and sends a ``t.me/<bot>?start=<token>``
link. When the user taps it, ``/start <token>`` consumes the payload with
:meth:`Cache.pop`, so each link works exactly once and only until its TTL.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from akkiabot_core.cache import Cache

logger = logging.getLogger(__name__)

KEY_PREFIX = "start:"
# Telegram allows up to 64 chars of A-Z a-z 0-9 _ - in a start parameter.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")


class StartPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str = Field(min_length=1, max_length=32)
    data: dict[str, Any] = Field(default_factory=dict)


class StartPayloadStore:
    def __init__(self, cache: Cache, *, default_ttl: int = 600) -> None:
        self._cache = cache
        self._default_ttl = default_ttl

    async def create(
        self, kind: str, data: dict[str, Any] | None = None, *, ttl: int | None = None
    ) -> str:
        """Store a payload and return its one-time token."""
        payload = StartPayload(kind=kind, data=data or {})
        token = secrets.token_urlsafe(24)  # 32 chars
        await self._cache.set(
            KEY_PREFIX + token, payload.model_dump_json(), ttl or self._default_ttl
        )
        return token

    async def consume(self, token: str) -> StartPayload | None:
        """Return the payload for ``token`` and invalidate it; ``None`` when the
        token is malformed, unknown, expired or already used."""
        if not _TOKEN.fullmatch(token):
            return None
        raw = await self._cache.pop(KEY_PREFIX + token)
        if raw is None:
            return None
        try:
            return StartPayload.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            logger.warning("discarding malformed start payload")
            return None


def start_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"
