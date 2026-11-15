"""Framework-agnostic Telegram helpers (no python-telegram-bot imports)."""

from akkiabot_core.telegram.display import public_name
from akkiabot_core.telegram.init_data import (
    InitDataError,
    WebAppChat,
    WebAppInitData,
    WebAppUser,
    parse_init_data,
    sign_init_data,
    verify_init_data,
)
from akkiabot_core.telegram.markdown import escape_markdown_v2, escape_markdown_v2_code

__all__ = [
    "InitDataError",
    "WebAppChat",
    "WebAppInitData",
    "WebAppUser",
    "escape_markdown_v2",
    "escape_markdown_v2_code",
    "parse_init_data",
    "public_name",
    "sign_init_data",
    "verify_init_data",
]
