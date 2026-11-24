import asyncio
import io
import logging
import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from akkiabot_core.cache import MemoryCache
from akkiabot_core.config import Settings
from akkiabot_core.db.migrate import upgrade_engine
from akkiabot_core.observability import configure_logging
from akkiabot_server.api import create_app
from akkiabot_server.api.app import SECRET_HEADER
from akkiabot_server.bot import polling
from akkiabot_server.bot.application import ALLOWED_UPDATES
from akkiabot_server.runtime import Runtime, polling_task
from akkiabot_server.testing import ApiError, FakeBotApi, UpdateFactory, metric_value

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_WEBHOOK_SECRET = "test-webhook-secret_123"
# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_BOT_TOKEN = "123456:fake-bot-token-for-tests"  # same as conftest


@asynccontextmanager
async def serve(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan (starts the bot) and give an HTTP client for it."""
    app = create_app(runtime)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


@pytest.fixture
def build(api: FakeBotApi) -> Callable[[Settings], Runtime]:
    def make(settings: Settings) -> Runtime:
        return Runtime.build(settings, cache=MemoryCache(), request=api, get_updates_request=api)

    return make


async def _wait_for(predicate: Callable[[], bool]) -> None:
    """Poll a condition set by a background task (the PTB update fetcher)."""
    async with asyncio.timeout(2.0):
        while not predicate():  # noqa: ASYNC110 - no event to await on the fake API
            await asyncio.sleep(0.01)


async def test_health_and_readiness(
    settings: Settings, build: Callable[[Settings], Runtime]
) -> None:
    async with serve(build(settings)) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ok",
            "checks": {"db": True, "cache": True, "polling": True},
        }


async def test_readiness_reports_failing_dependency(
    settings: Settings, build: Callable[[Settings], Runtime], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build(settings)

    async def broken() -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr(runtime.cache, "ping", broken)
    async with serve(runtime) as client:
        ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {
        "status": "unavailable",
        "checks": {"db": True, "cache": False, "polling": True},
    }


async def test_polling_mode_requests_explicit_updates(
    settings: Settings, api: FakeBotApi, build: Callable[[Settings], Runtime]
) -> None:
    polls = metric_value("akkiabot_telegram_poll_total", result="ok")
    runtime = build(settings)
    async with serve(runtime) as client:
        await _wait_for(lambda: bool(api.calls_to("getUpdates")))
        updater = runtime.application.updater
        assert updater is not None
        assert polling_task(updater) is not None  # the watchdog found PTB's task
        assert (await client.post("/telegram/webhook", json={})).status_code == 404
        # Every long poll feeds the "bot stopped receiving updates" alert.
        await _wait_for(lambda: metric_value("akkiabot_telegram_poll_total", result="ok") > polls)
        assert metric_value("akkiabot_telegram_poll_last_success_timestamp_seconds") > 0
    assert api.calls_to("getUpdates")[0]["allowed_updates"] == list(ALLOWED_UPDATES)
    assert set(ALLOWED_UPDATES) == {
        "message",
        "callback_query",
        "chat_member",
        "my_chat_member",
        "chat_join_request",
    }
    assert api.calls_to("setWebhook") == []
    assert not runtime.application.running


@pytest.fixture
def webhook_settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(
        telegram={
            "mode": "webhook",
            "webhook_url": "https://bot-api.example.com/",
            "webhook_secret": MOCK_WEBHOOK_SECRET,
        }
    )


async def test_webhook_mode(
    webhook_settings: Settings, api: FakeBotApi, build: Callable[[Settings], Runtime]
) -> None:
    runtime = build(webhook_settings)
    await upgrade_engine(runtime.engine)
    async with serve(runtime) as client:
        (registered,) = api.calls_to("setWebhook")
        assert registered == {
            "url": "https://bot-api.example.com/telegram/webhook",
            "secret_token": MOCK_WEBHOOK_SECRET,
            "allowed_updates": list(ALLOWED_UPDATES),
        }
        assert runtime.application.updater is None

        update = UpdateFactory(runtime.application.bot).command("/help").to_dict()
        assert (await client.post("/telegram/webhook", json=update)).status_code == 403
        bad = await client.post("/telegram/webhook", json=update, headers={SECRET_HEADER: "wrong"})
        assert bad.status_code == 403
        malformed = await client.post(
            "/telegram/webhook", content=b"not json", headers={SECRET_HEADER: MOCK_WEBHOOK_SECRET}
        )
        assert malformed.status_code == 400

        ok = await client.post(
            "/telegram/webhook", json=update, headers={SECRET_HEADER: MOCK_WEBHOOK_SECRET}
        )
        assert ok.status_code == 200
        await _wait_for(lambda: bool(api.sent_texts()))
    assert api.sent_texts()[0].startswith("可用命令列表")
    assert api.calls_to("getUpdates") == []


async def test_metrics_listener_is_separate(
    make_settings: Callable[..., Settings], api: FakeBotApi, build: Callable[[Settings], Runtime]
) -> None:
    settings = make_settings(
        observability={"metrics_enabled": True, "metrics_host": "127.0.0.1", "metrics_port": 0}
    )
    runtime = build(settings)
    await upgrade_engine(runtime.engine)
    async with serve(runtime) as client:
        port = runtime.metrics_port
        assert port
        await runtime.application.process_update(
            UpdateFactory(runtime.application.bot).command("/id")
        )
        async with httpx.AsyncClient() as metrics_client:
            body = (await metrics_client.get(f"http://127.0.0.1:{port}/metrics")).text
        # Not exposed on the public API port.
        assert (await client.get("/metrics")).status_code == 404

    assert 'akkiabot_handler_duration_seconds_count{handler="command:id"}' in body
    assert 'akkiabot_command_total{command="id",result="ok"}' in body
    assert 'akkiabot_updates_total{type="message"}' in body
    assert runtime.metrics_port is None  # listener closed on shutdown


async def test_metrics_disabled(settings: Settings, build: Callable[[Settings], Runtime]) -> None:
    runtime = build(settings)
    async with serve(runtime):
        assert runtime.metrics_port is None


async def test_startup_failure_releases_resources(
    make_settings: Callable[..., Settings], api: FakeBotApi, build: Callable[[Settings], Runtime]
) -> None:
    api.fail("getMe", "Unauthorized", status=401)
    runtime = build(make_settings(observability={"metrics_enabled": True, "metrics_port": 0}))
    stream = io.StringIO()
    configure_logging("INFO", fmt="json", stream=stream)

    with pytest.raises(RuntimeError, match="InvalidToken") as exc_info:
        async with serve(runtime):
            pass

    assert runtime.metrics_port is None
    # The bot token never shows up in the error, its traceback or the logs.
    rendered = "".join(traceback.format_exception(exc_info.value)) + stream.getvalue()
    assert MOCK_BOT_TOKEN not in rendered
    assert "bot startup failed: InvalidToken: The token `***` was rejected" in rendered


def _reject_token_on_polls(api: FakeBotApi, *, times: int | None = None) -> list[int]:
    """Answer ``getUpdates`` with 401 (PTB: ``InvalidToken``), ``times`` times or forever."""
    rejected: list[int] = []

    def respond(_params: dict[str, Any]) -> Any:
        if times is None or len(rejected) < times:
            rejected.append(1)
            raise ApiError("Unauthorized", status=401)
        return []

    api.respond("getUpdates", respond)
    return rejected


async def test_aborted_polling_turns_readiness_red_and_requests_exit(
    settings: Settings,
    api: FakeBotApi,
    build: Callable[[Settings], Runtime],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(polling, "TOKEN_REJECTED_RETRY_DELAYS", (0.0, 0.0))
    rejected = _reject_token_on_polls(api)
    runtime = build(settings)
    exits: list[bool] = []
    runtime.request_exit = lambda: exits.append(True)
    caplog.set_level(logging.INFO)

    async with serve(runtime) as client:
        await _wait_for(lambda: bool(exits))
        ready = await client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["checks"] == {"db": True, "cache": True, "polling": False}
    # The lifespan exit (Runtime.stop) above did not raise.

    assert len(rejected) == 3  # the first poll and two retries
    assert runtime.polling_failed
    assert exits == [True]
    assert not runtime.application.running
    # One error for the incident (PTB aborting polling): stopping the updater
    # re-raises it, but that is not logged (and so not sent to Sentry) again.
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert [r.getMessage() for r in errors] == [
        "Network Retry Loop (Polling Updates): Invalid token. Aborting retry loop."
    ]
    assert "telegram updater stopped after polling failure" in caplog.messages
    assert MOCK_BOT_TOKEN not in caplog.text


async def test_transient_token_rejection_keeps_polling(
    settings: Settings,
    api: FakeBotApi,
    build: Callable[[Settings], Runtime],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(polling, "TOKEN_REJECTED_RETRY_DELAYS", (0.0, 0.0))
    rejected = _reject_token_on_polls(api, times=2)
    runtime = build(settings)
    exits: list[bool] = []
    runtime.request_exit = lambda: exits.append(True)

    async with serve(runtime) as client:
        await _wait_for(lambda: len(api.calls_to("getUpdates")) >= 4)
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["polling"] is True

    assert len(rejected) == 2
    assert not runtime.polling_failed
    assert exits == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert caplog.messages.count("telegram rejected the bot token while polling; retrying") == 2
