"""``/start``, ``/help`` and ``/id``."""

from __future__ import annotations

import logging

from telegram import Message, Update
from telegram.constants import ParseMode

from akkiabot_core.telegram import escape_markdown_v2
from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.types import Context

logger = logging.getLogger(__name__)

START_GREETING = "你好，我是 AkkiaBot。发送 /help 查看可用命令。"
START_INVALID = "链接已失效或已被使用，请回到群里重新获取。"


async def start_command(update: Update, context: Context) -> str | None:
    message = update.effective_message
    if message is None:
        return "ignored"
    if not context.args:
        await message.reply_text(START_GREETING)
        return None

    deps = get_deps(context)
    payload = await deps.start_payloads.consume(context.args[0])
    handler = deps.start_handlers.get(payload.kind) if payload else None
    if payload is None or handler is None:
        if payload is not None:
            logger.warning("no handler for start payload", extra={"kind": payload.kind})
        await message.reply_text(START_INVALID)
        return "invalid_payload"
    await handler(update, context, payload)
    return None


async def help_command(update: Update, context: Context) -> str | None:
    message = update.effective_message
    if message is None:
        return "ignored"
    commands = get_deps(context).commands
    text = commands.command_help(context.args[0]) if context.args else commands.help_text()
    await message.reply_text(text)
    return None


def _replied_message(message: Message) -> Message | None:
    reply = message.reply_to_message
    # In forum topics every message implicitly "replies" to the topic header.
    if reply is None or reply.forum_topic_created is not None:
        return None
    return reply


async def id_command(update: Update, _context: Context) -> str | None:
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None:
        return "ignored"

    lines = [f"当前ChatId: {escape_markdown_v2(chat.effective_name or '')} \\(`{chat.id}`\\)"]
    if user is not None:
        lines.append(f"你的UserId: {escape_markdown_v2(user.first_name)} \\(`{user.id}`\\)")
    reply = _replied_message(message)
    if reply is not None:
        # Anonymous admins and channels post as a chat (from_user is a placeholder bot).
        if reply.sender_chat is not None:
            target_chat = reply.sender_chat
            name = escape_markdown_v2(target_chat.effective_name or "")
            lines.append(f"回复目标的ChatId: {name} \\(`{target_chat.id}`\\)")
        elif reply.from_user is not None:
            target = reply.from_user
            lines.append(
                f"回复目标的UserId: {escape_markdown_v2(target.first_name)} \\(`{target.id}`\\)"
            )
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
    return None
