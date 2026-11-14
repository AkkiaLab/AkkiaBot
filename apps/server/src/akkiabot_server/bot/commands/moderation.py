"""``/ban`` and ``/warn``: used as a reply, by the owner or admins allowed to ban."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Message, Update, User
from telegram.constants import ParseMode

from akkiabot_core.telegram import escape_markdown_v2
from akkiabot_server.bot.commands.basic import replied_message
from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.moderation_api import PtbModerationApi
from akkiabot_server.bot.types import Context
from akkiabot_server.services.chats import GROUP_TYPES
from akkiabot_server.services.moderation import (
    Authorization,
    MemberInfo,
    MemberRole,
    ModerationApiError,
    RateLimitedError,
    WarnDisabledError,
    authorize,
)

logger = logging.getLogger(__name__)
_e = escape_markdown_v2

BAN_USAGE = "/ban（回复要封禁的成员的消息）"
BAN_DOCS = (
    f"用法：{BAN_USAGE}\n封禁被回复消息的发送者，并删除机器人记录的该成员的历史消息。\n"
    "仅群主或具有封禁成员权限的管理员可用，不能作用于管理员。"
)
WARN_USAGE = "/warn（回复要警告的成员的消息）"
WARN_DOCS = (
    f"用法：{WARN_USAGE}\n警告被回复消息的发送者；警告次数达到上限后自动封禁并清零。\n"
    "需要先通过 /config 开启警告功能；仅群主或具有封禁成员权限的管理员可用，不能作用于管理员。"
)

GROUP_ONLY = "请在群组中使用此命令。"
ANONYMOUS_ACTOR = "匿名管理员无法使用此命令，请先关闭匿名身份再试。"
UNSUPPORTED_TARGET = "无法对频道或匿名管理员的消息执行此操作。"
SELF_TARGET = "不能对机器人自己执行此操作。"
LOOKUP_FAILED = "获取成员信息失败，请稍后再试。"
WARN_DISABLED = "请先通过 /config 开启警告功能。"


@dataclass(frozen=True, slots=True)
class _Request:
    message: Message
    chat_id: int
    actor: User
    target: User
    target_message: Message


async def _prepare(update: Update, context: Context, verb: str, usage: str) -> _Request | str:
    """Validate a ``/ban`` / ``/warn`` request; on rejection reply and return a result label."""
    message, chat, actor = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None or actor is None:
        return "ignored"
    if chat.type not in GROUP_TYPES:
        await message.reply_text(GROUP_ONLY)
        return "private"
    target_message = replied_message(message)
    if target_message is None:
        await message.reply_text(
            f"此命令需要以回复的方式使用，将{verb}被回复的成员。\n用法：{usage}"
        )
        return "usage"
    if message.sender_chat is not None:
        await message.reply_text(ANONYMOUS_ACTOR)
        return "anonymous"
    target = target_message.from_user
    if target_message.sender_chat is not None or target is None:
        await message.reply_text(UNSUPPORTED_TARGET)
        return "unsupported_target"
    if target.id == context.bot.id:
        await message.reply_text(SELF_TARGET)
        return "protected"

    api = PtbModerationApi(context.bot)
    try:
        actor_info = await api.get_member(chat.id, actor.id)
    except (ModerationApiError, RateLimitedError):
        logger.warning("moderation: actor lookup failed", exc_info=True)
        await message.reply_text(LOOKUP_FAILED)
        return "failed"
    target_info = await _target_info(api, chat.id, target.id)

    match authorize(actor_info, target_info):
        case Authorization.ACTOR_FORBIDDEN:
            await message.reply_text(
                f"你没有权限{verb}成员：仅群主或具有封禁成员权限的管理员可用。"
            )
            return "forbidden"
        case Authorization.TARGET_PROTECTED:
            await message.reply_text(f"无法{verb}管理员或群主。")
            return "protected"
        case Authorization.ALLOWED:
            return _Request(message, chat.id, actor, target, target_message)


async def _target_info(api: PtbModerationApi, chat_id: int, user_id: int) -> MemberInfo:
    try:
        return await api.get_member(chat_id, user_id)
    except (ModerationApiError, RateLimitedError):
        # Typically the user already left; they cannot be an admin then.
        logger.warning("moderation: target lookup failed, treating as member", exc_info=True)
        return MemberInfo(MemberRole.MEMBER)


def mention(user: User) -> str:
    """MarkdownV2 mention link."""
    return f"[{escape_markdown_v2(user.full_name)}](tg://user?id={user.id})"


async def ban_command(update: Update, context: Context) -> str | None:
    request = await _prepare(update, context, "封禁", BAN_USAGE)
    if isinstance(request, str):
        return request
    try:
        result = await get_deps(context).moderation.ban(
            PtbModerationApi(context.bot),
            request.chat_id,
            request.target.id,
            by=request.actor.id,
            extra_message_ids=[request.target_message.message_id],
        )
    except (ModerationApiError, RateLimitedError):
        logger.warning("ban failed", exc_info=True)
        await request.message.reply_text("封禁失败，请确认机器人具有封禁成员权限。")
        return "failed"
    text = mention(request.target) + _e(f" 已被封禁，删除了 {result.purge.deleted} 条消息。")
    if result.purge.failed:
        text += _e(f"\n另有 {result.purge.failed} 条消息删除失败。")
    await request.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    return None


async def warn_command(update: Update, context: Context) -> str | None:
    message, chat = update.effective_message, update.effective_chat
    # Like botlab-core: tell everyone (not only admins) when the feature is off.
    if (
        message is not None
        and chat is not None
        and chat.type in GROUP_TYPES
        and not (await get_deps(context).chat_settings.get(chat.id)).warn
    ):
        await message.reply_text(WARN_DISABLED)
        return "disabled"
    request = await _prepare(update, context, "警告", WARN_USAGE)
    if isinstance(request, str):
        return request
    try:
        result = await get_deps(context).moderation.warn(
            PtbModerationApi(context.bot), request.chat_id, request.target.id, by=request.actor.id
        )
    except WarnDisabledError:
        await request.message.reply_text(WARN_DISABLED)
        return "disabled"
    except (ModerationApiError, RateLimitedError):
        logger.warning("ban after warning limit failed", exc_info=True)
        await request.message.reply_text(
            "对方的警告次数已达上限，但封禁失败，请确认机器人具有封禁成员权限。"
        )
        return "failed"
    text = mention(request.target) + _e(f" 你已被警告 {result.count}/{result.limit} 次")
    if not result.banned:
        await request.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return None
    text += _e("，警告次数已达上限，已被封禁。")
    if result.purge is not None:
        text += _e(f"\n删除了 {result.purge.deleted} 条消息。")
    await request.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    return "banned"
