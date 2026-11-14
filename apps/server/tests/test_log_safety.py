"""The bot token must never reach the logs through the HTTP client.

Uses PTB's real ``HTTPXRequest`` over an ``httpx.MockTransport`` (no network)
and a fake token, with the root logger at DEBUG.
"""

import io
import json
import logging
from collections.abc import Callable, Iterator

import httpx
import pytest
from telegram.request import HTTPXRequest

from akkiabot_core.config import Settings
from akkiabot_core.observability import configure_logging
from akkiabot_server.runtime import Runtime
from akkiabot_server.testing import BOT_USER

FAKE_TOKEN = "987654:" + "Z" * 35


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    def reset() -> None:  # Runtime.build() changes these process-wide
        for name in ("httpx", "httpcore", "telegram.Bot", "telegram.ext.ExtBot"):
            logging.getLogger(name).setLevel(logging.NOTSET)

    reset()
    stream = io.StringIO()
    configure_logging("DEBUG", fmt="json", stream=stream)
    yield stream
    reset()


def _request(status: int, result: object) -> HTTPXRequest:
    def handler(request: httpx.Request) -> httpx.Response:
        ok = status == 200
        body = (
            {"ok": ok, "result": result}
            if ok
            else {"ok": False, "error_code": status, "description": "Unauthorized"}
        )
        return httpx.Response(status, content=json.dumps(body).encode())

    return HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(handler)})


def _settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(telegram={"bot_token": FAKE_TOKEN})


async def test_successful_requests_do_not_log_token(
    make_settings: Callable[..., Settings], log_stream: io.StringIO
) -> None:
    request = _request(200, BOT_USER)
    runtime = Runtime.build(_settings(make_settings), request=request, get_updates_request=request)
    await runtime.application.initialize()  # getMe through httpx
    try:
        assert runtime.application.bot.username == BOT_USER["username"]
    finally:
        await runtime.application.shutdown()
        await runtime.engine.dispose()

    logs = log_stream.getvalue()
    assert '"level": "debug"' in logs  # DEBUG logging is really on
    assert FAKE_TOKEN not in logs


async def test_failed_requests_do_not_log_token(
    make_settings: Callable[..., Settings], log_stream: io.StringIO
) -> None:
    request = _request(401, None)
    runtime = Runtime.build(_settings(make_settings), request=request, get_updates_request=request)
    with pytest.raises(RuntimeError, match="InvalidToken"):
        await runtime.start()

    assert FAKE_TOKEN not in log_stream.getvalue()


def test_unguarded_loggers_would_leak(log_stream: io.StringIO) -> None:
    """Documents why the guard exists: both log the token-bearing URL."""
    transport = httpx.MockTransport(lambda _r: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        client.get(f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe")
    from telegram import Bot

    Bot(FAKE_TOKEN)
    lines = [line for line in log_stream.getvalue().splitlines() if FAKE_TOKEN in line]
    assert {json.loads(line)["logger"] for line in lines} == {"httpx", "telegram.Bot"}
