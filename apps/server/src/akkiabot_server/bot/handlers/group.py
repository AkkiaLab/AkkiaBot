"""Group message handling: message log, service message cleanup, chat records."""

from __future__ import annotations

import logging

from telegram import Message, Update
from telegram.error import BadRequest, Forbidden, TelegramError

from akkiabot_core.observability import count_telegram_api_error
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


# Bot API error descriptions that mean the notice cannot or need not be
# deleted, mapped to the reason we log. They come from the group's own setup,
# Telegram's deletion limits, or someone else acting first; nothing on our side
# can fix them. Matched case-insensitively as substrings.
_EXPECTED_BAD_REQUESTS: tuple[tuple[str, str], ...] = (
    ("message to delete not found", "already_deleted"),
    ("not enough rights", "no_permission"),
    # Generic refusal: missing "delete messages" right, the deletion time limit,
    # or a notice Telegram does not allow deleting. The reply does not say which.
    ("message can't be deleted", "not_deletable"),
)
_EXPECTED_FORBIDDEN: tuple[tuple[str, str], ...] = (
    ("bot was kicked", "bot_not_in_chat"),
    ("bot is not a member", "bot_not_in_chat"),
)


def expected_delete_failure(exc: TelegramError) -> str | None:
    """Why deleting a service message failed, if that is expected.

    Returns a short reason (``already_deleted``, ``no_permission``,
    ``not_deletable``, ``bot_not_in_chat``) for failures caused by the group's
    configuration, Telegram's deletion limits or the notice already being gone,
    and ``None`` for real failures (network errors, timeouts, rate limits,
    unknown Bot API errors) that need attention.
    """
    if isinstance(exc, Forbidden):
        known = _EXPECTED_FORBIDDEN
    elif isinstance(exc, BadRequest):
        known = _EXPECTED_BAD_REQUESTS
    else:
        return None
    description = exc.message.lower()
    for fragment, reason in known:
        if fragment in description:
            return reason
    return None


async def handle_service_message(update: Update, context: Context) -> None:
    """Join/leave/pin… notices: make sure the chat is known, then delete the
    notice if the group enabled ``delete_service_messages``."""
    message = update.message
    if message is None:
        return
    deps = get_deps(context)
    await deps.messages.record(to_incoming(message, is_service=True))
    settings = await deps.chat_settings.get(message.chat.id)
    if not settings.delete_service_messages:
        return
    try:
        await message.delete()
    except TelegramError as exc:
        reason = expected_delete_failure(exc)
        if reason is None:
            count_telegram_api_error("deleteMessage")
            logger.warning(
                "failed to delete service message",
                extra={"message_id": message.message_id, "error": str(exc)},
            )
        else:
            logger.info(
                "service message not deleted",
                extra={"message_id": message.message_id, "reason": reason, "error": str(exc)},
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
