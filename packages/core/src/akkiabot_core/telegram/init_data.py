"""Validation of Telegram Mini App ``initData``.

See https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app.
Unlike the previous implementation this also rejects stale payloads based on
``auth_date``, so a leaked initData string cannot be replayed indefinitely.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")

# Clock skew tolerated for auth_date values slightly in the future.
_FUTURE_SKEW = timedelta(seconds=60)


class InitDataError(ValueError):
    """initData is malformed, forged or expired. ``reason`` is log-safe."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WebAppUser(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None
    language_code: str | None = None
    is_premium: bool | None = None
    is_bot: bool | None = None


class WebAppChat(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    type: str
    title: str = ""
    username: str | None = None


class WebAppInitData(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    auth_date: datetime
    hash: str
    query_id: str | None = None
    user: WebAppUser | None = None
    receiver: WebAppUser | None = None
    chat: WebAppChat | None = None
    chat_type: str | None = None
    chat_instance: str | None = None
    start_param: str | None = None
    can_send_after: int | None = None


_JSON_FIELDS = ("user", "receiver", "chat")


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Compute the ``hash`` Telegram would attach to ``fields`` (for tests/tools)."""
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return hmac.new(_secret_key(bot_token), check_string.encode(), hashlib.sha256).hexdigest()


def parse_init_data(
    raw: str,
    bot_token: str,
    *,
    max_age: timedelta,
    now: datetime | None = None,
) -> WebAppInitData:
    """Verify and parse ``raw``; raise :class:`InitDataError` on any problem."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise InitDataError("malformed") from None
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError("duplicate_field")
        fields[key] = value

    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("missing_hash")
    # compare_digest raises TypeError on non-ASCII str, so validate the shape first.
    if not _HASH_RE.fullmatch(received_hash):
        raise InitDataError("bad_signature")
    expected = sign_init_data(fields, bot_token)
    if not hmac.compare_digest(expected.encode(), received_hash.lower().encode()):
        raise InitDataError("bad_signature")

    try:
        auth_ts = int(fields["auth_date"])
    except (KeyError, ValueError):
        raise InitDataError("missing_auth_date") from None
    auth_date = datetime.fromtimestamp(auth_ts, UTC)
    current = now or datetime.now(UTC)
    if current - auth_date > max_age:
        raise InitDataError("expired")
    if auth_date - current > _FUTURE_SKEW:
        raise InitDataError("auth_date_in_future")

    data: dict[str, object] = {**fields, "hash": received_hash, "auth_date": auth_date}
    try:
        for key in _JSON_FIELDS:
            if key in fields:
                data[key] = json.loads(fields[key])
        return WebAppInitData.model_validate(data)
    except (ValueError, ValidationError):
        raise InitDataError("invalid_payload") from None


def verify_init_data(
    raw: str,
    bot_token: str,
    *,
    max_age: timedelta,
    now: datetime | None = None,
) -> WebAppInitData | None:
    """Like :func:`parse_init_data` but returns ``None`` on failure (logged)."""
    try:
        return parse_init_data(raw, bot_token, max_age=max_age, now=now)
    except InitDataError as exc:
        logger.info("initData rejected", extra={"reason": exc.reason})
        return None
