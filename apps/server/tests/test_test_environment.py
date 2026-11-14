"""Bot API endpoint selection: production by default, ``/test`` only when configured."""

from collections.abc import Callable
from typing import Any

from akkiabot_core.config import Settings
from akkiabot_server.runtime import Runtime

TOKEN = "123456:" + "A" * 35  # same as conftest


def _bot_urls(make_settings: Callable[..., Settings], **telegram: Any) -> tuple[str, str]:
    bot = Runtime.build(make_settings(telegram=telegram)).application.bot
    return bot.base_url, bot.base_file_url


def test_production_endpoints_by_default(make_settings: Callable[..., Settings]) -> None:
    assert _bot_urls(make_settings) == (
        f"https://api.telegram.org/bot{TOKEN}",
        f"https://api.telegram.org/file/bot{TOKEN}",
    )


def test_test_environment_uses_test_endpoints_only(make_settings: Callable[..., Settings]) -> None:
    assert _bot_urls(make_settings, test_environment=True) == (
        f"https://api.telegram.org/bot{TOKEN}/test",
        f"https://api.telegram.org/file/bot{TOKEN}/test",
    )
