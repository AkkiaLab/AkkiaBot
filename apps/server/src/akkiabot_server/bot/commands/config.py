"""``/config``: group switches edited through inline buttons in a private chat.

Flow: ``/config`` in a group checks the sender's rights
and replies with a deep link → the link opens a private chat and ``/start``
consumes the one-time payload (bound to that group *and* that user) → the bot
shows one toggle button per switch → each button press re-checks the rights in
the group before changing anything.
"""

from __future__ import annotations

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest

from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.moderation_api import PtbModerationApi
from akkiabot_server.bot.types import Context
from akkiabot_server.services.chat_settings import (
    TOGGLE_LABELS,
    ChatConfig,
    Toggle,
    UnknownChatError,
)
from akkiabot_server.services.chats import GROUP_TYPES
from akkiabot_server.services.moderation import ModerationApiError, RateLimitedError
from akkiabot_server.services.start_payload import StartPayload, start_link

logger = logging.getLogger(__name__)

START_KIND = "config"
CALLBACK_PREFIX = "cfg:"
CALLBACK_PATTERN = re.compile(r"^cfg:")
"""Routes every ``/config`` button here; malformed data is answered, not ignored."""
_CALLBACK_DATA = re.compile(r"cfg:(-?\d+):([a-z_]+):([01])")

NOT_PERMITTED = "需要群主或具有封禁成员权限的管理员才能使用 /config。"
ANONYMOUS = "匿名管理员无法使用此命令，请先关闭匿名身份再试。"
PRIVATE_HINT = "请在需要配置的群组中发送 /config 命令。"
WRONG_USER = "验证失败：您不是发起配置的用户。"
LOOKUP_FAILED = "获取群组信息失败，机器人可能不在群内。"
ENTRY_TEXT = "功能设置"
ENTRY_BUTTON = "👉点击按钮开始设置👈"
CALLBACK_INVALID = "无效按钮"
CALLBACK_FORBIDDEN = "需要群主或具有封禁成员权限的管理员才能修改设置"
CALLBACK_DONE = "设置成功"


def menu(config: ChatConfig) -> tuple[str, InlineKeyboardMarkup]:
    """Menu text and one toggle button per switch; a button carries the *next* state."""
    rows = []
    for toggle, label in TOGGLE_LABELS.items():
        enabled = bool(getattr(config.settings, toggle.value))
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if enabled else '❌'} {label}",
                    callback_data=f"{CALLBACK_PREFIX}{config.chat_id}:{toggle.value}:{int(not enabled)}",
                )
            ]
        )
    text = f"群名：{config.title or config.chat_id}\n功能开关（点击切换）"
    return text, InlineKeyboardMarkup(rows)


async def _can_configure(context: Context, chat_id: int, user_id: int) -> bool | None:
    """Whether the user may configure the group; ``None`` when Telegram can't tell us."""
    try:
        member = await PtbModerationApi(context.bot).get_member(chat_id, user_id)
    except (ModerationApiError, RateLimitedError):
        logger.warning("config: member lookup failed", exc_info=True)
        return None
    return member.can_moderate


async def config_command(update: Update, context: Context) -> str | None:
    message, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if message is None or chat is None or user is None:
        return "ignored"
    if chat.type not in GROUP_TYPES:
        await message.reply_text(PRIVATE_HINT)
        return "private"
    if message.sender_chat is not None:
        await message.reply_text(ANONYMOUS)
        return "anonymous"
    allowed = await _can_configure(context, chat.id, user.id)
    if allowed is None:
        await message.reply_text(LOOKUP_FAILED)
        return "failed"
    if not allowed:
        await message.reply_text(NOT_PERMITTED)
        return "forbidden"

    deps = get_deps(context)
    token = await deps.start_payloads.create(START_KIND, {"chat_id": chat.id, "user_id": user.id})
    button = InlineKeyboardButton(ENTRY_BUTTON, url=start_link(context.bot.username, token))
    await message.reply_text(ENTRY_TEXT, reply_markup=InlineKeyboardMarkup([[button]]))
    return None


async def config_start(update: Update, context: Context, payload: StartPayload) -> None:
    """``/start <token>`` of a ``/config`` link: show the menu to the same admin."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    chat_id, owner = payload.data.get("chat_id"), payload.data.get("user_id")
    if not isinstance(chat_id, int) or owner != user.id:
        logger.warning("config link used by another user")
        await message.reply_text(WRONG_USER)
        return
    allowed = await _can_configure(context, chat_id, user.id)
    if not allowed:
        await message.reply_text(LOOKUP_FAILED if allowed is None else NOT_PERMITTED)
        return
    config = await get_deps(context).chat_settings.describe(chat_id)
    if config is None:
        await message.reply_text(LOOKUP_FAILED)
        return
    text, markup = menu(config)
    await message.reply_text(text, reply_markup=markup)


async def config_callback(update: Update, context: Context) -> None:
    """A toggle button was pressed: re-check the rights, apply, redraw the menu."""
    query = update.callback_query
    if query is None:
        return
    match = _CALLBACK_DATA.fullmatch(query.data or "")
    if match is None or match.group(2) not in Toggle:
        await query.answer(CALLBACK_INVALID)
        return
    chat_id, toggle, enabled = int(match.group(1)), Toggle(match.group(2)), match.group(3) == "1"

    allowed = await _can_configure(context, chat_id, query.from_user.id)
    if not allowed:
        logger.warning(
            "config toggle rejected",
            extra={
                "target_chat_id": chat_id,
                "toggle": toggle.value,
                "lookup_failed": allowed is None,
            },
        )
        await query.answer(
            LOOKUP_FAILED if allowed is None else CALLBACK_FORBIDDEN, show_alert=True
        )
        return

    settings_service = get_deps(context).chat_settings
    try:
        await settings_service.set_toggle(chat_id, toggle, enabled)
    except UnknownChatError:
        await query.answer(LOOKUP_FAILED, show_alert=True)
        return
    logger.info(
        "config toggled",
        extra={"target_chat_id": chat_id, "toggle": toggle.value, "enabled": enabled},
    )
    config = await settings_service.describe(chat_id)
    if config is not None and query.message is not None:
        text, markup = menu(config)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            # "message is not modified": a double tap already showed this state.
            if "not modified" not in str(exc).lower():
                raise
    await query.answer(CALLBACK_DONE)
