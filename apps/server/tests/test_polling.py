"""The getUpdates heartbeat that backs the "bot stopped receiving updates" alert."""

from __future__ import annotations

import logging
from typing import Any, override

import pytest
from telegram.error import BadRequest, Conflict, NetworkError, TelegramError
from telegram.request import BaseRequest, RequestData

from akkiabot_server.bot.polling import (
    PollHeartbeatRequest,
    default_get_updates_request,
    log_polling_error,
)
from akkiabot_server.testing import metric_value

BASE = "https://api.telegram.org/botTOKEN/test"


class Scripted(BaseRequest):
    """Returns ``status`` or raises ``error`` for every call."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[str, Any]] = []
        self.lifecycle: list[str] = []

    @property
    @override
    def read_timeout(self) -> float | None:
        return 7.0

    @override
    async def initialize(self) -> None:
        self.lifecycle.append("initialize")

    @override
    async def shutdown(self) -> None:
        self.lifecycle.append("shutdown")

    @override
    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        self.calls.append((url, read_timeout))
        if self.error is not None:
            raise self.error
        return self.status, b'{"ok": true, "result": []}'


def _counts() -> tuple[float, float]:
    return (
        metric_value("akkiabot_telegram_poll_total", result="ok"),
        metric_value("akkiabot_telegram_poll_total", result="error"),
    )


async def test_successful_poll_refreshes_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    inner = Scripted()
    request = PollHeartbeatRequest(inner)
    ok, err = _counts()

    status, _ = await request.do_request(f"{BASE}/getUpdates", "POST", read_timeout=30)

    assert status == 200
    assert inner.calls == [(f"{BASE}/getUpdates", 30)]
    assert _counts() == (ok + 1, err)
    assert metric_value("akkiabot_telegram_poll_last_success_timestamp_seconds") == 1_800_000_000.0


async def test_failed_poll_counts_error_and_keeps_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.setattr(time, "time", lambda: 1_800_000_100.0)
    await PollHeartbeatRequest(Scripted()).do_request(f"{BASE}/getUpdates", "POST")
    monkeypatch.setattr(time, "time", lambda: 1_800_000_500.0)
    ok, err = _counts()

    status, _ = await PollHeartbeatRequest(Scripted(status=502)).do_request(
        f"{BASE}/getUpdates", "POST"
    )
    assert status == 502
    with pytest.raises(OSError, match="network down"):
        await PollHeartbeatRequest(Scripted(error=OSError("network down"))).do_request(
            f"{BASE}/getUpdates", "POST"
        )

    assert _counts() == (ok, err + 2)
    assert metric_value("akkiabot_telegram_poll_last_success_timestamp_seconds") == 1_800_000_100.0


async def test_other_methods_are_not_counted() -> None:
    ok, err = _counts()
    await PollHeartbeatRequest(Scripted()).do_request(f"{BASE}/sendMessage", "POST")
    assert _counts() == (ok, err)


async def test_delegates_lifecycle_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.setattr(time, "time", lambda: 1_800_001_000.0)
    inner = Scripted()
    request = PollHeartbeatRequest(inner)
    await request.initialize()
    # Polling starts the staleness clock even before the first successful poll.
    assert metric_value("akkiabot_telegram_poll_last_success_timestamp_seconds") == 1_800_001_000.0
    await request.shutdown()
    assert inner.lifecycle == ["initialize", "shutdown"]
    assert request.read_timeout == 7.0


def test_default_request_matches_ptb_default() -> None:
    # PTB's builder uses a single connection for getUpdates.
    request = default_get_updates_request()
    assert request._client_kwargs["limits"].max_connections == 1


class Sequenced(Scripted):
    """Answers with ``statuses`` in turn, then 200."""

    def __init__(self, *statuses: int) -> None:
        super().__init__()
        self.statuses = list(statuses)

    @override
    async def do_request(
        self, url: str, method: str, *args: Any, **kwargs: Any
    ) -> tuple[int, bytes]:
        await super().do_request(url, method)
        return (self.statuses.pop(0) if self.statuses else 200), b"{}"


async def test_rejected_token_is_retried_before_giving_up() -> None:
    inner = Sequenced(401, 404, 401, 401)
    ok, err = _counts()

    status, _ = await PollHeartbeatRequest(inner, retry_delays=(0, 0, 0)).do_request(
        f"{BASE}/getUpdates", "POST"
    )

    assert status == 401  # PTB now raises InvalidToken and stops polling
    assert len(inner.calls) == 4
    assert _counts() == (ok, err + 4)


async def test_transient_token_rejection_recovers() -> None:
    inner = Sequenced(401)
    status, _ = await PollHeartbeatRequest(inner, retry_delays=(0, 0, 0)).do_request(
        f"{BASE}/getUpdates", "POST"
    )
    assert status == 200
    assert len(inner.calls) == 2


async def test_only_get_updates_and_token_rejections_are_retried() -> None:
    other = Sequenced(401)
    status, _ = await PollHeartbeatRequest(other, retry_delays=(0,)).do_request(
        f"{BASE}/sendMessage", "POST"
    )
    assert (status, len(other.calls)) == (401, 1)
    server_error = Sequenced(502)
    status, _ = await PollHeartbeatRequest(server_error, retry_delays=(0,)).do_request(
        f"{BASE}/getUpdates", "POST"
    )
    assert (status, len(server_error.calls)) == (502, 1)


def test_default_retry_is_short() -> None:
    # A revoked token must still stop polling (and restart the process) quickly.
    assert sum(PollHeartbeatRequest(Scripted()).retry_delays) <= 10


@pytest.mark.parametrize(
    "error",
    [
        NetworkError("Bad Gateway"),
        NetworkError("httpx.ConnectError: All connection attempts failed"),
        NetworkError("httpx.RemoteProtocolError: Server disconnected without sending a response."),
    ],
)
def test_transient_polling_errors_are_warnings(
    error: NetworkError, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        log_polling_error(error)
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert getattr(record, "detail", None) == error.message


@pytest.mark.parametrize(
    "error",
    [BadRequest("Bad Request: wrong parameter"), Conflict("Conflict"), TelegramError("unknown")],
)
def test_other_polling_errors_stay_errors(
    error: TelegramError, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        log_polling_error(error)
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert record.exc_info[1] is error
