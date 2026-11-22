"""Join captcha telemetry: what the Mini App reports about one verification page.

Two complementary signals, both keyed by the challenge id so the whole flow can
be followed in Loki (``| challenge_id="…"``):

* :class:`ClientInfo` travels with ``POST /api/v1/captcha/verify``. A random
  ``page_id`` is generated on every page load and ``attempt`` counts submissions
  within that page, so a resubmission from the same page (same ``page_id``,
  ``attempt`` > 1) can be told apart from a submission after reopening the Mini
  App (new ``page_id``; the signed initData ``auth_date`` changes too).
* :class:`ClientEvent` is reported by ``POST /api/v1/captcha/events`` for the
  page lifecycle (opened → Turnstile solved → submitted → result → closed),
  which covers members who open the page but never submit.

Only fixed enum values and short, pattern-checked strings are accepted; the
Turnstile token and the raw initData are never logged. Users are identified by
the ``chat_id`` / ``user_id`` of the challenge, as in every other captcha log.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class ClientEvent(StrEnum):
    """Lifecycle events of the captcha page (``/api/v1/captcha/events``)."""

    PAGE_LOADED = "page_loaded"
    """The page rendered the Turnstile widget (all preconditions met)."""
    PAGE_BLOCKED = "page_blocked"
    """The page could not show the widget (e.g. missing site key)."""
    TURNSTILE_SOLVED = "turnstile_solved"
    TURNSTILE_ERROR = "turnstile_error"
    TURNSTILE_EXPIRED = "turnstile_expired"
    SUBMITTED = "submitted"
    RESULT = "result"
    """The verify response arrived; ``outcome`` says which."""
    RETRY = "retry"
    """The member pressed "retry" after a retryable failure."""
    CLOSED = "closed"
    """The page was closed or hidden (best effort; may not arrive)."""


class ClientOutcome(StrEnum):
    """``outcome`` of a :attr:`ClientEvent.RESULT` event: what the page showed."""

    PASSED = "passed"
    NETWORK_ERROR = "network_error"
    UNEXPECTED_RESPONSE = "unexpected_response"
    # Rejections, same values as the verify API error codes (RejectReason).
    CAPTCHA_UNAVAILABLE = "captcha_unavailable"
    TURNSTILE_FAILED = "turnstile_failed"
    INIT_DATA_INVALID = "init_data_invalid"
    INIT_DATA_EXPIRED = "init_data_expired"
    CHALLENGE_NOT_FOUND = "challenge_not_found"
    CHALLENGE_EXPIRED = "challenge_expired"
    CHALLENGE_USED = "challenge_used"
    WRONG_USER = "wrong_user"
    NOT_CAPTCHA_RESTRICTED = "not_captcha_restricted"
    UNMUTE_FAILED = "unmute_failed"


class EventRejectReason(StrEnum):
    """Why ``/api/v1/captcha/events`` refused a report (API error codes)."""

    INIT_DATA_INVALID = "init_data_invalid"
    INIT_DATA_EXPIRED = "init_data_expired"
    CHALLENGE_NOT_FOUND = "challenge_not_found"
    """Unknown challenge, or it belongs to someone else (not told apart)."""
    RATE_LIMITED = "rate_limited"
    INVALID_EVENT = "invalid_event"
    """``outcome`` given without a ``result`` event, or missing on one."""


class EventRejectedError(Exception):
    def __init__(self, reason: EventRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


#: Telegram.WebApp.platform values are short lowercase words (android, ios,
#: tdesktop, weba, macos, unknown…); anything else is refused by the API.
PLATFORM_PATTERN = r"^[a-z_]{1,16}$"
#: Telegram.WebApp.version, e.g. "8.0".
VERSION_PATTERN = r"^[0-9]{1,2}(\.[0-9]{1,3}){0,2}$"


@dataclass(frozen=True, slots=True)
class ClientInfo:
    """What the page says about itself; every field is optional (older pages)."""

    page_id: uuid.UUID | None = None
    attempt: int | None = None
    platform: str | None = None
    version: str | None = None

    def log_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "page_id": str(self.page_id) if self.page_id else None,
            "attempt": self.attempt,
            "client_platform": self.platform,
            "client_version": self.version,
        }
        return {k: v for k, v in fields.items() if v is not None}


class _ExpiringCounter:
    """Per-key counters that reset ``window`` seconds after a key is first seen.

    In-process only: the bot runs as a single instance (see the member lock in
    :mod:`akkiabot_server.services.captcha`). The map is bounded; once full,
    new keys are refused until expired ones are pruned.
    """

    def __init__(
        self, *, window: float, max_keys: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._window = window
        self._max_keys = max_keys
        self._clock = clock
        self._entries: dict[object, tuple[float, int, float]] = {}
        """key → (window start, count, last hit)."""

    def hit(self, key: object) -> tuple[int, float | None] | None:
        """Count one hit. Returns ``(count in window, seconds since previous hit)``,
        or ``None`` when the map is full and ``key`` is new."""
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and now - entry[0] >= self._window:
            entry = None
        if entry is None:
            if key not in self._entries and len(self._entries) >= self._max_keys:
                self._prune(now)
                if len(self._entries) >= self._max_keys:
                    return None
            self._entries[key] = (now, 1, now)
            return 1, None
        start, count, last = entry
        self._entries[key] = (start, count + 1, now)
        return count + 1, now - last

    def _prune(self, now: float) -> None:
        for key in [k for k, (start, _, _) in self._entries.items() if now - start >= self._window]:
            del self._entries[key]


class EventRateLimiter:
    """At most ``limit`` event reports per challenge per ``window`` seconds."""

    def __init__(
        self,
        *,
        limit: int = 40,
        window: float = 600,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._counter = _ExpiringCounter(window=window, max_keys=max_keys, clock=clock)

    def allow(self, challenge_id: uuid.UUID) -> bool:
        hit = self._counter.hit(challenge_id)
        return hit is not None and hit[0] <= self._limit


class AttemptClock:
    """Seconds since the previous verify request for the same challenge."""

    def __init__(
        self,
        *,
        window: float = 3600,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._counter = _ExpiringCounter(window=window, max_keys=max_keys, clock=clock)

    def since_previous(self, challenge_id: uuid.UUID) -> float | None:
        hit = self._counter.hit(challenge_id)
        return None if hit is None else hit[1]
