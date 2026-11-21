"""``/ban`` and ``/warn``: used as a reply, by the owner or admins allowed to ban."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Message, ReplyParameters, Update, User
from telegram.constants import ParseMode
from telegram.error import TelegramError

from akkiabot_core.telegram import escape_markdown_v2, public_name
from akkiabot_server.bot.commands.basic import replied_message
from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.moderation_api import PtbModerationApi
from akkiabot_server.bot.types import Context
from akkiabot_server.services.chats import GROUP_TYPES
from akkiabot_server.services.moderation import (
    Authorization,
    MemberInfo,
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


async def reply(message: Message, text: str, *, parse_mode: str | None = None) -> Message:
    """Reply to ``message``, or send plainly to its chat if it is gone by now.

    The command message can disappear while the command runs (deleted by its
    author, another bot or an admin); quoting it would then fail with
    "Message to be replied not found" (BOT-48).
    """
    return await message.reply_text(
        text,
        parse_mode=parse_mode,
        reply_parameters=ReplyParameters(message.message_id, allow_sending_without_reply=True),
    )


async def report_done(message: Message, text: str) -> None:
    """Send the receipt of an action that already took effect.

    A failed receipt must not turn the finished action into a failed command:
    that would report an error and invite a retry (a second ban).
    """
    try:
        await reply(message, text, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError:
        logger.warning("moderation: failed to send the receipt", exc_info=True)


@dataclass(frozen=True, slots=True)
class Request:
    message: Message
    chat_id: int
    actor: User
    target: User
    target_message: Message
    actor_info: MemberInfo
    target_info: MemberInfo


async def resolve(update: Update, context: Context, verb: str, usage: str) -> Request | str:
    """Validate a reply-style moderation command and look up both members.

    On rejection reply and return a result label (permissions are not checked).
    """
    message, chat, actor = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None or actor is None:
        return "ignored"
    if chat.type not in GROUP_TYPES:
        await reply(message, GROUP_ONLY)
        return "private"
    target_message = replied_message(message)
    if target_message is None:
        await reply(message, f"此命令需要以回复的方式使用，将{verb}被回复的成员。\n用法：{usage}")
        return "usage"
    if message.sender_chat is not None:
        await reply(message, ANONYMOUS_ACTOR)
        return "anonymous"
    target = target_message.from_user
    if target_message.sender_chat is not None or target is None:
        await reply(message, UNSUPPORTED_TARGET)
        return "unsupported_target"
    if target.id == context.bot.id:
        await reply(message, SELF_TARGET)
        return "protected"

    api = PtbModerationApi(context.bot)
    try:
        actor_info = await api.get_member(chat.id, actor.id)
        # Users who left are reported explicitly (``left`` / ``kicked``). Any error
        # means the target's rights are unknown, so never assume "plain member".
        target_info = await api.get_member(chat.id, target.id)
    except (ModerationApiError, RateLimitedError):
        logger.warning("moderation: member lookup failed", exc_info=True)
        await reply(message, LOOKUP_FAILED)
        return "failed"

    return Request(message, chat.id, actor, target, target_message, actor_info, target_info)


async def _prepare(update: Update, context: Context, verb: str, usage: str) -> Request | str:
    """Validate a ``/ban`` / ``/warn`` request; on rejection reply and return a result label."""
    request = await resolve(update, context, verb, usage)
    if isinstance(request, str):
        return request
    message = request.message
    match authorize(request.actor_info, request.target_info):
        case Authorization.ACTOR_FORBIDDEN:
            await reply(message, f"你没有权限{verb}成员：仅群主或具有封禁成员权限的管理员可用。")
            return "forbidden"
        case Authorization.TARGET_PROTECTED:
            await reply(message, f"无法{verb}管理员或群主。")
            return "protected"
        case Authorization.ALLOWED:
            return request


def mention(user: User) -> str:
    """MarkdownV2 mention of the handled user: only a fragment of the name is shown."""
    return f"[{escape_markdown_v2(public_name(user.full_name))}](tg://user?id={user.id})"


async def ban_command(update: Update, context: Context) -> str | None:
    request = await _prepare(update, context, "封禁", BAN_USAGE)
    if isinstance(request, str):
        return request
    return await execute_ban(request, context)


async def execute_ban(request: Request, context: Context, *, note: str = "") -> str | None:
    """Ban an authorized request's target and report the result in the chat."""
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
        await reply(request.message, "封禁失败，请确认机器人具有封禁成员权限。")
        return "failed"
    text = mention(request.target) + _e(f" 已被封禁{note}，删除了 {result.purge.deleted} 条消息。")
    if result.purge.failed:
        text += _e(f"\n另有 {result.purge.failed} 条消息删除失败。")
    await report_done(request.message, text)
    return None


async def warn_command(update: Update, context: Context) -> str | None:
    message, chat = update.effective_message, update.effective_chat
    # Tell everyone (not only admins) when the feature is off.
    if (
        message is not None
        and chat is not None
        and chat.type in GROUP_TYPES
        and not (await get_deps(context).chat_settings.get(chat.id)).warn
    ):
        await reply(message, WARN_DISABLED)
        return "disabled"
    request = await _prepare(update, context, "警告", WARN_USAGE)
    if isinstance(request, str):
        return request
    try:
        result = await get_deps(context).moderation.warn(
            PtbModerationApi(context.bot), request.chat_id, request.target.id, by=request.actor.id
        )
    except WarnDisabledError:
        await reply(request.message, WARN_DISABLED)
        return "disabled"
    except (ModerationApiError, RateLimitedError):
        logger.warning("ban after warning limit failed", exc_info=True)
        await reply(
            request.message, "对方的警告次数已达上限，但封禁失败，请确认机器人具有封禁成员权限。"
        )
        return "failed"
    text = mention(request.target) + _e(f" 你已被警告 {result.count}/{result.limit} 次")
    if not result.banned:
        await report_done(request.message, text)
        return None
    text += _e("，警告次数已达上限，已被封禁。")
    if result.purge is not None:
        text += _e(f"\n删除了 {result.purge.deleted} 条消息。")
    await report_done(request.message, text)
    return "banned"
