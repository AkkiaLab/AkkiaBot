"""Build the python-telegram-bot :class:`Application` with all handlers."""

from __future__ import annotations

import logging
from typing import Final

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import BaseRequest

from akkiabot_server.bot.captcha import captcha_start, handle_chat_member, schedule_captcha_jobs
from akkiabot_server.bot.commands.config import CALLBACK_PATTERN, config_callback, config_start
from akkiabot_server.bot.commands.config import START_KIND as CONFIG_START_KIND
from akkiabot_server.bot.deps import BotDeps, attach_deps
from akkiabot_server.bot.handlers.group import (
    handle_my_chat_member,
    handle_service_message,
    record_group_message,
)
from akkiabot_server.bot.instrumentation import count_incoming_update, instrument, on_error
from akkiabot_server.bot.jobs import schedule_jobs
from akkiabot_server.bot.registry import CommandRegistry
from akkiabot_server.bot.types import App
from akkiabot_server.services.captcha import START_KIND as CAPTCHA_START_KIND

logger = logging.getLogger(__name__)

ALLOWED_UPDATES: Final = ("message", "callback_query", "chat_member", "my_chat_member")
"""Requested explicitly: ``chat_member`` is not delivered unless asked for."""

# Handler groups run in ascending order; every group gets its chance at an update.
GROUP_METRICS: Final = -1000
GROUP_BOOKKEEPING: Final = -100
GROUP_COMMANDS: Final = 0


#: Telegram test environment endpoints (``{token}`` is filled in by PTB).
BOT_API_TEST_URL = "https://api.telegram.org/bot{token}/test"
BOT_API_TEST_FILE_URL = "https://api.telegram.org/file/bot{token}/test"


def build_application(
    deps: BotDeps,
    *,
    webhook: bool,
    request: BaseRequest | None = None,
    get_updates_request: BaseRequest | None = None,
) -> App:
    """Create the application. ``request`` overrides the HTTP layer (tests)."""
    builder = ApplicationBuilder().token(deps.settings.telegram.bot_token.get_secret_value())
    if deps.settings.telegram.test_environment:
        builder = builder.base_url(BOT_API_TEST_URL).base_file_url(BOT_API_TEST_FILE_URL)
    if request is not None:
        builder = builder.request(request)
    if get_updates_request is not None:
        builder = builder.get_updates_request(get_updates_request)
    if webhook:
        builder = builder.updater(None)
    application = builder.build()
    attach_deps(application, deps)
    deps.on_start(CONFIG_START_KIND, config_start)
    # Reusable: a stranger tapping the group prompt must not burn the link.
    deps.on_start(CAPTCHA_START_KIND, captcha_start, reusable=True)
    register_handlers(application, deps.commands)
    schedule_jobs(application, cleanup_interval=deps.settings.telegram.message_log_cleanup_interval)
    schedule_captcha_jobs(application)
    return application


def register_handlers(application: App, commands: CommandRegistry) -> None:
    application.add_handler(TypeHandler(Update, count_incoming_update), group=GROUP_METRICS)

    groups = filters.ChatType.GROUPS
    application.add_handlers(
        [
            MessageHandler(
                groups & filters.StatusUpdate.ALL,
                instrument("service_message", handle_service_message),
            ),
            MessageHandler(
                groups & ~filters.StatusUpdate.ALL,
                instrument("record_message", record_group_message),
            ),
            ChatMemberHandler(
                instrument("my_chat_member", handle_my_chat_member),
                ChatMemberHandler.MY_CHAT_MEMBER,
            ),
            ChatMemberHandler(
                instrument("chat_member", handle_chat_member),
                ChatMemberHandler.CHAT_MEMBER,
            ),
        ],
        group=GROUP_BOOKKEEPING,
    )
    application.add_handlers(
        [
            *commands.handlers(),
            CallbackQueryHandler(
                instrument("callback:config", config_callback), pattern=CALLBACK_PATTERN
            ),
        ],
        group=GROUP_COMMANDS,
    )
    application.add_error_handler(on_error)


async def sync_commands(application: App, commands: CommandRegistry) -> None:
    """Publish the command menu. Failure is logged, not fatal."""
    try:
        await application.bot.set_my_commands(commands.bot_commands())
    except TelegramError:
        logger.exception("set_my_commands failed")
