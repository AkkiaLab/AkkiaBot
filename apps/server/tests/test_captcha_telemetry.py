"""Unit tests for the in-process counters behind captcha page telemetry."""

from __future__ import annotations

import uuid

from akkiabot_server.services.captcha_telemetry import (
    AttemptClock,
    ClientInfo,
    EventRateLimiter,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limit_is_per_challenge_and_resets_after_the_window() -> None:
    clock = FakeClock()
    limiter = EventRateLimiter(limit=2, window=60, clock=clock)
    a, b = uuid.uuid4(), uuid.uuid4()
    assert [limiter.allow(a) for _ in range(3)] == [True, True, False]
    assert limiter.allow(b)
    clock.now += 60
    assert limiter.allow(a)


def test_rate_limiter_stays_bounded() -> None:
    clock = FakeClock()
    limiter = EventRateLimiter(limit=5, window=60, max_keys=2, clock=clock)
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert limiter.allow(first)
    assert limiter.allow(second)
    assert not limiter.allow(third)  # full, nothing expired yet
    assert limiter.allow(first)  # known keys keep their budget
    clock.now += 60
    assert limiter.allow(third)  # expired entries were pruned


def test_attempt_clock_measures_the_gap_between_requests() -> None:
    clock = FakeClock()
    attempts = AttemptClock(window=600, clock=clock)
    challenge = uuid.uuid4()
    assert attempts.since_previous(challenge) is None
    clock.now += 10
    assert attempts.since_previous(challenge) == 10
    clock.now += 2.5
    assert attempts.since_previous(challenge) == 2.5
    clock.now += 600
    assert attempts.since_previous(challenge) is None


def test_client_info_logs_only_what_was_sent() -> None:
    assert ClientInfo().log_fields() == {}
    page = uuid.uuid4()
    assert ClientInfo(page, 0, "ios", "8.0").log_fields() == {
        "page_id": str(page),
        "attempt": 0,
        "client_platform": "ios",
        "client_version": "8.0",
    }
