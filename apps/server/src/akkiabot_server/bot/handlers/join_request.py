"""Pending join requests: tell the applicant to wait for an admin.

When someone asks to join a chat through a link that requires approval, the
bot sends them one private message. It never approves or
declines the request itself; that stays with the chat's admins. This is the
stage *before* the user is a member; the join captcha only starts once the
request is approved and the user actually joins (``chat_member``).

Telegram only delivers ``chat_join_request`` to bots that are admins with the
"invite users" right, and lets the bot message the applicant (via
``user_chat_id``) for 5 minutes or until the request is handled, even if the
user never started the bot.
"""

from __future__ import annotations

import logging
from typing import Final

from telegram import Update
from telegram.error import TelegramError

from akkiabot_core.observability import count_telegram_api_error
from akkiabot_server.bot.types import Context

logger = logging.getLogger(__name__)

#: Fixed text; the e2e acceptance checks it word for word.
PENDING_APPROVAL_TEXT: Final = "Hello, please wait for the admin to approve your request."


async def handle_join_request(update: Update, context: Context) -> None:
    request = update.chat_join_request
    if request is None:
        return
    try:
        await context.bot.send_message(request.user_chat_id, PENDING_APPROVAL_TEXT)
    except TelegramError as exc:
        # E.g. the applicant blocked the bot, or the 5-minute window expired.
        # Nothing to retry: the request itself is unaffected.
        count_telegram_api_error("sendMessage")
        logger.warning(
            "join request notice failed",
            extra={"chat_type": request.chat.type, "error": str(exc)},
        )
        return
    logger.info("join request notice sent", extra={"chat_type": request.chat.type})
