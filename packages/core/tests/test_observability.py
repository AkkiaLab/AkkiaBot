from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
from collections.abc import Iterator
from typing import Any

import pytest

from akkiabot_core.config import ObservabilitySettings
from akkiabot_core.observability import (
    REGISTRY,
    configure_logging,
    count_command,
    count_update,
    count_whois_lookup,
    get_log_context,
    init_sentry,
    log_context,
    new_trace_id,
    render_metrics,
    setup_observability,
    track_handler,
)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    level = root.level
    stream = io.StringIO()
    handler = configure_logging("DEBUG", stream=stream)
    yield stream
    for h in [h for h in root.handlers if h.get_name() == handler.get_name()]:
        root.removeHandler(h)
    root.setLevel(level)


def records(stream: io.StringIO) -> list[dict[str, Any]]:
    """Records emitted by the test logger "t" (ignores e.g. asyncio debug lines)."""
    parsed = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [r for r in parsed if r["logger"] == "t"]


def sample(name: str, **labels: str) -> float:
    value = REGISTRY.get_sample_value(f"akkiabot_{name}", labels)
    return value or 0.0


# ------------------------------------------------------------------ logging


def test_json_log_line(log_stream: io.StringIO) -> None:
    logging.getLogger("t").info("hello %s", "世界", extra={"foo": 1})
    (rec,) = records(log_stream)
    assert rec["level"] == "info"
    assert rec["logger"] == "t"
    assert rec["message"] == "hello 世界"
    assert rec["foo"] == 1
    assert rec["ts"].endswith("+00:00")


def test_context_fields_injected(log_stream: io.StringIO) -> None:
    log = logging.getLogger("t")
    trace_id = new_trace_id()
    with log_context(chat_id=-1001234567890, user_id=42, update_id=7, trace_id=trace_id):
        log.info("inside")
        with log_context(handler="cmd_id", user_id=43):
            log.info("nested")
        log.info("after nested")
    log.info("outside")

    inside, nested, after, outside = records(log_stream)
    assert inside["chat_id"] == -1001234567890
    assert inside["user_id"] == 42
    assert inside["update_id"] == 7
    assert inside["trace_id"] == trace_id
    assert len(trace_id) == 32
    assert nested["handler"] == "cmd_id"
    assert nested["user_id"] == 43
    assert "handler" not in after
    assert after["user_id"] == 42
    assert "chat_id" not in outside


def test_none_fields_are_skipped() -> None:
    with log_context(chat_id=1, user_id=None):
        assert dict(get_log_context()) == {"chat_id": 1}


async def test_context_isolated_between_tasks(log_stream: io.StringIO) -> None:
    log = logging.getLogger("t")

    async def handle(update_id: int) -> None:
        with log_context(update_id=update_id):
            await asyncio.sleep(0.01 * (3 - update_id))
            log.info("done")

    await asyncio.gather(*(handle(i) for i in range(3)))
    for rec in records(log_stream):
        assert rec["message"] == "done"
    assert sorted(r["update_id"] for r in records(log_stream)) == [0, 1, 2]


def test_exception_logged(log_stream: io.StringIO) -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("t").exception("failed")
    (rec,) = records(log_stream)
    assert "RuntimeError: boom" in rec["exc_info"]


# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_RAW = 'p"a\\ss\nw\u00f6rd 123456:fake-bot-token-for-tests'

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_SENTRY_DSN = "https://key@o0.ingest.sentry.io/1"


def test_content_is_logged_verbatim(log_stream: io.StringIO) -> None:
    """The logging layer never rewrites content; redaction is the caller's job."""
    log = logging.getLogger("t")
    log.info("value %s", MOCK_RAW)
    log.info("extra", extra={"cfg": {"db": [MOCK_RAW]}, "obj": RuntimeError(MOCK_RAW)})
    try:
        raise ValueError(MOCK_RAW)
    except ValueError:
        log.exception("failed")

    message, extra, exc = records(log_stream)
    assert message["message"] == f"value {MOCK_RAW}"
    assert extra["cfg"] == {"db": [MOCK_RAW]}
    assert extra["obj"] == MOCK_RAW
    assert f"ValueError: {MOCK_RAW}" in exc["exc_info"]


def test_console_format(log_stream: io.StringIO) -> None:
    stream = io.StringIO()
    handler = configure_logging("INFO", fmt="console", stream=stream)
    try:
        with log_context(chat_id=5):
            logging.getLogger("t").info("hi %s", "there")
    finally:
        logging.getLogger().removeHandler(handler)
    line = stream.getvalue()
    assert "hi there" in line
    assert "[chat_id=5]" in line


def test_configure_logging_is_idempotent() -> None:
    root = logging.getLogger()
    configure_logging("INFO", stream=io.StringIO())
    handler = configure_logging("INFO", stream=io.StringIO())
    try:
        assert sum(h.get_name() == "akkiabot" for h in root.handlers) == 1
    finally:
        root.removeHandler(handler)


# ------------------------------------------------------------------ metrics


async def test_track_handler_success(log_stream: io.StringIO) -> None:
    @track_handler("unit_ok")
    async def handler(x: int) -> int:
        logging.getLogger("t").info("in handler")
        return x * 2

    before = sample("handler_duration_seconds_count", handler="unit_ok")
    assert await handler(21) == 42
    assert sample("handler_duration_seconds_count", handler="unit_ok") == before + 1
    assert sample("handler_errors_total", handler="unit_ok") == 0
    assert records(log_stream)[0]["handler"] == "unit_ok"
    assert handler.__name__ == "handler"


async def test_track_handler_error(log_stream: io.StringIO) -> None:
    @track_handler("unit_err")
    async def handler() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        await handler()
    assert sample("handler_errors_total", handler="unit_err") == 1
    assert sample("handler_duration_seconds_count", handler="unit_err") == 1
    (rec,) = [
        r for r in map(json.loads, log_stream.getvalue().splitlines()) if r["level"] == "error"
    ]
    assert rec["handler"] == "unit_err"


async def test_track_handler_bare_uses_qualname() -> None:
    @track_handler
    async def bare_handler() -> str:
        return "ok"

    assert await bare_handler() == "ok"
    label = "test_track_handler_bare_uses_qualname.<locals>.bare_handler"
    assert sample("handler_duration_seconds_count", handler=label) == 1


def test_track_handler_rejects_sync() -> None:
    def sync() -> None: ...

    with pytest.raises(TypeError):
        track_handler("x")(sync)  # pyright: ignore[reportArgumentType]


def test_counters_and_exposition() -> None:
    before = sample("command_total", command="unit", result="ok")
    count_command("unit")
    count_update("message")
    assert sample("command_total", command="unit", result="ok") == before + 1
    body, content_type = render_metrics()
    text = body.decode()
    assert content_type.startswith("text/plain")
    assert 'akkiabot_command_total{command="unit",result="ok"}' in text
    assert "akkiabot_updates_total" in text
    assert "process_cpu_seconds_total" in text or "python_info" in text


def test_whois_lookup_counter() -> None:
    before = sample("whois_lookup_total", provider="ipinfo", result="ok")
    count_whois_lookup("ipinfo", "ok")
    assert sample("whois_lookup_total", provider="ipinfo", result="ok") == before + 1


# ------------------------------------------------------------------- sentry


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def guard(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise AssertionError(f"unexpected network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: guard(None, a))  # pyright: ignore[reportArgumentType]
    return attempts


@pytest.mark.usefixtures("log_stream")
def test_startup_without_sentry_dsn_makes_no_requests(no_network: list[object]) -> None:
    import sentry_sdk

    settings = ObservabilitySettings()
    assert init_sentry(settings) is False
    setup_observability(settings)
    logging.getLogger("t").error("an error that would be reported if Sentry were on")
    assert not sentry_sdk.is_initialized()
    assert no_network == []


def test_sentry_initialised_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    settings = ObservabilitySettings(
        sentry_dsn=MOCK_SENTRY_DSN,  # pyright: ignore[reportArgumentType]
        sentry_environment="production",
        sentry_traces_sample_rate=0.1,
    )
    assert init_sentry(settings, release="akkiabot@0.1.0") is True
    assert captured["dsn"] == MOCK_SENTRY_DSN
    assert captured["environment"] == "production"
    assert captured["traces_sample_rate"] == 0.1
    assert captured["release"] == "akkiabot@0.1.0"
    # SDK defaults: no PII is collected.
    assert "send_default_pii" not in captured
    assert "data_collection" not in captured
