"""The getUpdates heartbeat that backs the "bot stopped receiving updates" alert."""

from __future__ import annotations

from typing import Any, override

import pytest
from telegram.request import BaseRequest, RequestData

from akkiabot_server.bot.polling import PollHeartbeatRequest, default_get_updates_request
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
