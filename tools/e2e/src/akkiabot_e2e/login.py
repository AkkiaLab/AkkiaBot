"""First-time interactive login of a test account (owner types code / 2FA locally)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from akkiabot_e2e.errors import SafetyError
from akkiabot_e2e.files import config_home
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.transport import UserInfo

Prompt = Callable[[str], str]
Ask = Callable[[], str]


class LoginClient(Protocol):
    async def login(self, phone: Ask, code: Ask, password: Ask) -> UserInfo: ...

    def session_string(self) -> str: ...

    async def close(self) -> None: ...


def default_session_file(role: str) -> Path:
    return config_home() / "e2e" / f"{role}.session"


async def interactive_login(
    client: LoginClient, *, prompt: Prompt, secret_prompt: Prompt
) -> tuple[UserInfo, Secret]:
    """Log in; returns the user and the session (kept in memory, never printed)."""
    try:
        user = await client.login(
            lambda: prompt("测试环境账号手机号（含国家码，如 +8613800000000）："),
            lambda: prompt("Telegram 测试环境发来的验证码："),
            lambda: secret_prompt("两步验证密码（输入不回显）："),
        )
        if user.is_bot:
            raise SafetyError("登录的是 bot 账号，需要真实用户账号")
        return user, Secret(client.session_string())
    finally:
        await client.close()
