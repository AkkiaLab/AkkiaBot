import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from telegram import Update

from akkiabot_core.config import ObservabilitySettings
from akkiabot_core.observability import configure_logging, init_sentry
from akkiabot_server.bot.registry import Command, CommandRegistry
from akkiabot_server.bot.types import Context
from akkiabot_server.runtime import Runtime
from akkiabot_server.testing import FakeBotApi, UpdateFactory, metric_value

GROUP_ID = -1001234567890


class CapturingTransport(Transport):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(dict(event))


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Initialise Sentry through the app's own init path, capturing events in memory."""
    transport = CapturingTransport()
    real_init = sentry_sdk.init

    def init_with_transport(**kwargs: Any) -> Any:
        return real_init(transport=transport, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init_with_transport)
    settings = ObservabilitySettings(sentry_dsn="https://key@o0.ingest.sentry.io/1")  # pyright: ignore[reportArgumentType]
    assert init_sentry(settings, release="akkiabot@test")
    try:
        yield transport.events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


async def _boom(_update: Update, _context: Context) -> None:
    raise RuntimeError("kaboom")


@pytest.fixture
def boom(runtime: Runtime) -> None:
    runtime.application.add_handlers(
        CommandRegistry([Command("boom", "always fails", _boom)]).handlers()
    )


@pytest.mark.usefixtures("boom")
async def test_handler_error_is_counted_logged_and_contained(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    stream = io.StringIO()
    configure_logging("INFO", fmt="json", stream=stream)
    before = {
        "errors": metric_value("akkiabot_handler_errors_total", handler="command:boom"),
        "duration": metric_value("akkiabot_handler_duration_seconds_count", handler="command:boom"),
        "command": metric_value("akkiabot_command_total", command="boom", result="error"),
        "updates": metric_value("akkiabot_updates_total", type="message"),
    }

    update = updates.command("/boom", chat=updates.group(GROUP_ID), from_user=updates.user(42))
    await runtime.application.process_update(update)  # does not raise

    assert (
        metric_value("akkiabot_handler_errors_total", handler="command:boom")
        == before["errors"] + 1
    )
    assert (
        metric_value("akkiabot_handler_duration_seconds_count", handler="command:boom")
        == before["duration"] + 1
    )
    assert (
        metric_value("akkiabot_command_total", command="boom", result="error")
        == before["command"] + 1
    )
    assert metric_value("akkiabot_updates_total", type="message") == before["updates"] + 1

    failures = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["message"] == "handler failed"
    ]
    assert len(failures) == 1
    record = failures[0]
    assert record["level"] == "error"
    assert record["handler"] == "command:boom"
    assert record["update_id"] == update.update_id
    assert record["chat_id"] == GROUP_ID
    assert record["user_id"] == 42
    assert len(record["trace_id"]) == 32
    assert "RuntimeError: kaboom" in record["exc_info"]


@pytest.mark.usefixtures("boom")
async def test_handler_error_reaches_sentry_once(
    runtime: Runtime, updates: UpdateFactory, sentry_events: list[dict[str, Any]]
) -> None:
    logging.getLogger().setLevel(logging.INFO)
    await runtime.application.process_update(updates.command("/boom"))
    sentry_sdk.flush()

    errors = [e for e in sentry_events if e.get("level") == "error"]
    assert len(errors) == 1
    (exception,) = errors[0]["exception"]["values"]
    assert exception["type"] == "RuntimeError"
    assert exception["value"] == "kaboom"
    assert errors[0]["release"] == "akkiabot@test"


async def test_successful_command_metrics(runtime: Runtime, updates: UpdateFactory) -> None:
    before = metric_value("akkiabot_command_total", command="help", result="ok")
    await runtime.application.process_update(updates.command("/help"))
    assert metric_value("akkiabot_command_total", command="help", result="ok") == before + 1


async def test_updates_counted_by_type(runtime: Runtime, updates: UpdateFactory) -> None:
    before = metric_value("akkiabot_updates_total", type="my_chat_member")
    await runtime.application.process_update(updates.my_chat_member())
    assert metric_value("akkiabot_updates_total", type="my_chat_member") == before + 1
