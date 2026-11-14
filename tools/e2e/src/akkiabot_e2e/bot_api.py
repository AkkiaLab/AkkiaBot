"""Bot API checks against the Telegram *test* environment only.

Requests go to ``https://api.telegram.org/bot<token>/test/<method>``; nothing in
this module can address the production endpoints. The token is part of the
URL, so ``httpx`` / ``httpcore`` logging is kept at WARNING by the CLI.
"""

from __future__ import annotations

import logging

import httpx

from akkiabot_e2e.errors import SafetyError
from akkiabot_e2e.redaction import Secret

BOT_API_TEST_BASE = "https://api.telegram.org/bot{token}/test/{method}"
THIRD_PARTY_LOGGERS = ("httpx", "httpcore")


def quiet_http_logs() -> None:
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def bot_api_url(token: Secret, method: str) -> str:
    return BOT_API_TEST_BASE.format(token=token.reveal(), method=method)


async def check_test_bot(token: Secret, expected_username: str, client: httpx.AsyncClient) -> None:
    """``getMe`` on the test endpoint must succeed and return ``expected_username``."""
    try:
        response = await client.get(bot_api_url(token, "getMe"))
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise SafetyError(
            "无法在 Telegram 测试环境 Bot API 上校验 bot token（不会回退到生产环境）"
        ) from None
    result = data.get("result") if isinstance(data, dict) and data.get("ok") else None
    if not isinstance(result, dict):
        raise SafetyError(
            "bot token 在 Telegram 测试环境 Bot API 上不可用（可能是生产环境 token），"
            "拒绝运行，不会回退到生产环境"
        )
    username = str(result.get("username", ""))
    if username.lower() != expected_username.lower():
        raise SafetyError(f"测试环境 bot token 对应 @{username}，与 E2E_BOT_USERNAME 不一致")
