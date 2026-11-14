"""``/spam``: report a message (as a reply).

* Owner / administrators allowed to ban: the sender is banned right away.
* Ordinary members: the LLM judges the reported user's name, bio and message;
  spam → ban, otherwise every administrator is mentioned for a manual review.
  The review runs in the background and edits the bot's "report received"
  message with the result.
"""

from __future__ import annotations

import contextlib
import math

from telegram import Update

from akkiabot_core.observability import track_handler
from akkiabot_server.bot.commands.moderation import execute_ban, resolve
from akkiabot_server.bot.deps import BotDeps, get_deps
from akkiabot_server.bot.moderation_api import PtbAntiSpamApi
from akkiabot_server.bot.types import Context
from akkiabot_server.services.antispam_guard import AntiSpamApi, SpamReport

SPAM_USAGE = "/spam（回复要举报的消息）"
SPAM_DOCS = (
    f"用法：{SPAM_USAGE}\n"
    "群主或具有封禁成员权限的管理员使用时，直接封禁被回复消息的发送者并删除其历史消息；\n"
    "普通成员使用时，由机器人结合对方的名字、简介和消息内容判定：是垃圾信息则封禁，"
    "否则通知全部管理员人工审核。不能举报管理员。"
)
PROTECTED = "无法举报管理员或群主。"
SELF_REPORT = "不能举报自己。"
RECEIVED = "举报已收到，正在进行机器审核……"
NO_TEXT = "（无文字内容）"


@track_handler("spam_report")
async def _review(deps: BotDeps, api: AntiSpamApi, report: SpamReport) -> None:
    await deps.antispam.review_report(api, report)


async def _run(deps: BotDeps, api: AntiSpamApi, report: SpamReport) -> None:
    with contextlib.suppress(Exception):  # counted and logged by track_handler
        await _review(deps, api, report)


async def spam_command(update: Update, context: Context) -> str | None:
    request = await resolve(update, context, "举报", SPAM_USAGE)
    if isinstance(request, str):
        return request
    message = request.message
    if request.target_info.is_admin:
        await message.reply_text(PROTECTED)
        return "protected"
    if request.actor_info.can_moderate:
        return await execute_ban(request, context, note="（垃圾信息）") or "admin_ban"
    if request.target.id == request.actor.id:
        await message.reply_text(SELF_REPORT)
        return "self"

    deps = get_deps(context)
    wait = deps.spam_reports.acquire(request.actor.id)
    if wait > 0:
        await message.reply_text(f"举报过于频繁，请 {math.ceil(wait)} 秒后再试。")
        return "rate_limited"
    target_message = request.target_message
    status = await message.reply_text(RECEIVED)
    report = SpamReport(
        chat_id=request.chat_id,
        reporter_id=request.actor.id,
        target_id=request.target.id,
        target_name=request.target.full_name,
        message_id=target_message.message_id,
        text=target_message.text or target_message.caption or NO_TEXT,
        status_message_id=status.message_id,
        bot_id=context.bot.id,
    )
    deps.background.spawn(
        _run(deps, PtbAntiSpamApi(context.bot), report),
        name=f"antispam:report:{request.chat_id}:{target_message.message_id}",
    )
    return "reported"
