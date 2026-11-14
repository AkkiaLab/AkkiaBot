"""Group message handling: message log, service message cleanup, chat records."""

from __future__ import annotations

import logging

from telegram import Message, Update
from telegram.error import TelegramError

from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.types import Context
from akkiabot_server.services.chats import GROUP_TYPES
from akkiabot_server.services.message_log import IncomingMessage

logger = logging.getLogger(__name__)


def to_incoming(message: Message, *, is_service: bool) -> IncomingMessage:
    sender = message.from_user
    return IncomingMessage(
        chat_id=message.chat.id,
        chat_type=message.chat.type,
        chat_title=message.chat.title,
        message_id=message.message_id,
        sender_id=sender.id if sender else None,
        sender_is_bot=bool(sender and sender.is_bot),
        is_service=is_service,
        date=message.date,
    )


async def record_group_message(update: Update, context: Context) -> None:
    """Every non-service group message: store its id (``message_log``)."""
    message = update.message
    if message is None:
        return
    await get_deps(context).messages.record(to_incoming(message, is_service=False))


async def handle_service_message(update: Update, context: Context) -> None:
    """Join/leave/pin… notices: make sure the chat is known, then delete the
    notice if the group enabled ``delete_service_messages``."""
    message = update.message
    if message is None:
        return
    deps = get_deps(context)
    await deps.messages.record(to_incoming(message, is_service=True))
    settings = await deps.chats.settings(message.chat.id)
    if not settings.delete_service_messages:
        return
    try:
        await message.delete()
    except TelegramError as exc:
        # Typically: the bot is not an admin with "delete messages".
        logger.warning(
            "failed to delete service message",
            extra={"message_id": message.message_id, "error": str(exc)},
        )
    else:
        logger.info("service message deleted", extra={"message_id": message.message_id})


async def handle_my_chat_member(update: Update, context: Context) -> None:
    """The bot's own membership changed (added, promoted, removed…)."""
    change = update.my_chat_member
    if change is None or change.chat.type not in GROUP_TYPES:
        return
    logger.info(
        "bot membership changed",
        extra={
            "old_status": change.old_chat_member.status,
            "new_status": change.new_chat_member.status,
        },
    )
    await get_deps(context).chats.refresh(
        change.chat.id, type=change.chat.type, title=change.chat.title
    )
