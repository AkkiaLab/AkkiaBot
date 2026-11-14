"""Fixtures for the server: a real :class:`Runtime` on SQLite with a fake Bot API."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from akkiabot_core.cache import MemoryCache
from akkiabot_core.config import Settings
from akkiabot_core.db.migrate import upgrade_engine
from akkiabot_server.runtime import Runtime
from akkiabot_server.testing import FakeBotApi, UpdateFactory

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_BOT_TOKEN = "123456:fake-bot-token-for-tests"
_GROUPS = {
    "TELEGRAM",
    "DB",
    "REDIS",
    "LLM",
    "ANTISPAM",
    "LANGFUSE",
    "TURNSTILE",
    "WHOIS",
    "WEBAPP",
    "CORS",
    "OBSERVABILITY",
    "SERVER",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in list(os.environ):
        if key.split("_", 1)[0] in _GROUPS:
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


type MakeSettings = Callable[..., Settings]


@pytest.fixture
def make_settings(tmp_path: Path) -> MakeSettings:
    def make(
        *,
        telegram: dict[str, Any] | None = None,
        observability: dict[str, Any] | None = None,
        whois: dict[str, Any] | None = None,
        **groups: dict[str, Any],
    ) -> Settings:
        return Settings(
            telegram={"bot_token": MOCK_BOT_TOKEN, **(telegram or {})},  # pyright: ignore[reportArgumentType]
            db={"url": f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"},  # pyright: ignore[reportArgumentType]
            observability={"metrics_enabled": False, **(observability or {})},  # pyright: ignore[reportArgumentType]
            whois=whois or {},  # pyright: ignore[reportArgumentType]
            **groups,  # pyright: ignore[reportArgumentType]
        )

    return make


@pytest.fixture
def settings(make_settings: MakeSettings) -> Settings:
    return make_settings()


@pytest.fixture
def api() -> FakeBotApi:
    return FakeBotApi()


@pytest.fixture
async def runtime(settings: Settings, api: FakeBotApi) -> AsyncIterator[Runtime]:
    """A migrated, initialised (not polling) runtime; feed it updates with
    ``await runtime.application.process_update(update)``."""
    rt = Runtime.build(settings, cache=MemoryCache(), request=api, get_updates_request=api)
    await upgrade_engine(rt.engine)
    await rt.application.initialize()
    try:
        yield rt
    finally:
        await rt.deps.background.drain()
        await rt.deps.background.aclose()
        await rt.application.shutdown()
        await rt.http.aclose()
        await rt.engine.dispose()


@pytest.fixture
def updates(runtime: Runtime) -> UpdateFactory:
    return UpdateFactory(runtime.application.bot)
