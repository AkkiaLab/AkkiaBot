"""Acceptance configuration, read from environment variables.

In normal use ``op run --env-file tools/e2e/staging.env.tpl`` injects these
from the ``AkkiaBot-Staging`` vault; nothing is read from files in the repo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from akkiabot_e2e.errors import ConfigError
from akkiabot_e2e.redaction import Redactor, Secret

DEFAULT_CHAT_TITLE = "AkkiaBot E2E 自动验收群"
CHAT_ABOUT = "AkkiaBot 自动化验收专用测试群，由 akkiabot-e2e 创建与使用。请勿用于其他用途。"

#: Variables the live run cannot do without (names only are ever reported).
REQUIRED = (
    "E2E_API_ID",
    "E2E_API_HASH",
    "E2E_BOT_USERNAME",
    "E2E_ADMIN_SESSION",
    "E2E_MEMBER_SESSION",
    "E2E_ALLOWED_USER_IDS",
)
#: Values registered with the redactor (besides the sessions / api_hash above).
_OTHER_SECRETS = (
    "TELEGRAM_BOT_TOKEN",
    "WHOIS_IPINFO_TOKEN",
    "OBSERVABILITY_SENTRY_DSN",
    "OP_SERVICE_ACCOUNT_TOKEN",
)


@dataclass(frozen=True, slots=True)
class ApiCredentials:
    api_id: int
    api_hash: Secret


@dataclass(frozen=True, slots=True)
class E2EConfig:
    api: ApiCredentials
    bot_username: str
    admin_session: Secret
    member_session: Secret
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    chat_title: str = DEFAULT_CHAT_TITLE
    reply_timeout: float = 30.0
    flood_max_wait: float = 120.0
    send_interval: float = 2.0


def missing_variables(env: Mapping[str, str], names: tuple[str, ...] = REQUIRED) -> list[str]:
    return [name for name in names if not env.get(name, "").strip()]


def _int(env: Mapping[str, str], name: str) -> int:
    try:
        return int(env[name].strip())
    except ValueError:
        raise ConfigError(f"{name} 必须是整数") from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} 必须是数字") from None
    if value <= 0:
        raise ConfigError(f"{name} 必须大于 0")
    return value


def _ids(env: Mapping[str, str], name: str) -> frozenset[int]:
    raw = env.get(name, "")
    try:
        return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)
    except ValueError:
        raise ConfigError(f"{name} 必须是逗号分隔的整数 ID") from None


def load_api_credentials(env: Mapping[str, str]) -> ApiCredentials:
    missing = missing_variables(env, ("E2E_API_ID", "E2E_API_HASH"))
    if missing:
        raise ConfigError(f"缺少环境变量：{', '.join(missing)}")
    return ApiCredentials(_int(env, "E2E_API_ID"), Secret(env["E2E_API_HASH"].strip()))


def load_config(env: Mapping[str, str]) -> E2EConfig:
    """Parse the full config; call :func:`missing_variables` first to decide skipping."""
    missing = missing_variables(env)
    if missing:
        raise ConfigError(f"缺少环境变量：{', '.join(missing)}")
    allowed_users = _ids(env, "E2E_ALLOWED_USER_IDS")
    if len(allowed_users) < 2:
        raise ConfigError("E2E_ALLOWED_USER_IDS 至少需要群主和普通成员两个测试账号的 user id")
    return E2EConfig(
        api=load_api_credentials(env),
        bot_username=env["E2E_BOT_USERNAME"].strip().removeprefix("@"),
        admin_session=Secret(env["E2E_ADMIN_SESSION"].strip()),
        member_session=Secret(env["E2E_MEMBER_SESSION"].strip()),
        allowed_user_ids=allowed_users,
        allowed_chat_ids=_ids(env, "E2E_ALLOWED_CHAT_IDS"),
        chat_title=env.get("E2E_CHAT_TITLE", "").strip() or DEFAULT_CHAT_TITLE,
        reply_timeout=_float(env, "E2E_REPLY_TIMEOUT", 30.0),
        flood_max_wait=_float(env, "E2E_FLOOD_MAX_WAIT", 120.0),
        send_interval=_float(env, "E2E_SEND_INTERVAL", 2.0),
    )


def build_redactor(env: Mapping[str, str]) -> Redactor:
    """Redactor for every known secret present in ``env`` (even if config is incomplete)."""
    names = ("E2E_API_HASH", "E2E_ADMIN_SESSION", "E2E_MEMBER_SESSION", *_OTHER_SECRETS)
    return Redactor(env.get(name, "").strip() or None for name in names)
