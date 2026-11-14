"""Create the test bot through the test-environment @BotFather with the admin account."""

from __future__ import annotations

import asyncio
import re
import secrets
from collections.abc import Callable

from akkiabot_e2e.errors import CheckFailed, E2EError, SafetyError
from akkiabot_e2e.transport import Account, Msg

TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b")
BOT_NAME = "AkkiaBot E2E"
USERNAME_PREFIX = "akkiabot_e2e"


def random_username(prefix: str = USERNAME_PREFIX) -> str:
    return f"{prefix}_{secrets.token_hex(3)}_bot"


async def _next_from(
    account: Account, chat_id: int, sender_id: int, after: int, reply_timeout: float, poll: float
) -> Msg:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + reply_timeout
    while True:
        for msg in await account.history(chat_id, min_id=after):
            if msg.sender_id == sender_id:
                return msg
        if loop.time() >= deadline:
            raise CheckFailed(f"{reply_timeout:g} 秒内 BotFather 没有回复")
        await asyncio.sleep(poll)


async def create_test_bot(
    admin: Account,
    *,
    name: str = BOT_NAME,
    make_username: Callable[[], str] = random_username,
    attempts: int = 3,
    reply_timeout: float = 30.0,
    poll: float = 1.0,
) -> tuple[str, str]:
    """Return ``(username, token)`` of a new bot. The token is never logged here."""
    father = await admin.resolve_user("BotFather")
    if not father.is_bot:
        raise SafetyError("@BotFather 不是 bot，拒绝继续")

    async def say(text: str) -> Msg:
        sent = await admin.send(father.id, text)
        return await _next_from(admin, father.id, father.id, sent.id, reply_timeout, poll)

    await say("/newbot")
    await say(name)
    for _ in range(attempts):
        username = make_username()
        reply = await say(username)
        match = TOKEN_PATTERN.search(reply.text)
        if match:
            return username, match.group(0)
        if "/newbot" not in reply.text and "username" not in reply.text.lower():
            break
    raise E2EError(
        "BotFather 没有返回 bot token（用户名可能被占用或已达到 bot 数量上限），请查看测试账号与 BotFather 的对话"
    )
