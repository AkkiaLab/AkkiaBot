"""Bot side of the join captcha (see :mod:`akkiabot_server.services.captcha`).

* ``chat_member`` updates: a user becoming a member → mute + challenge; an
  admin lifting a restriction → ``chat_member`` synced.
* ``/start <token>`` of the group prompt → Mini App button for the challenged user.
* JobQueue: a one-off timeout per challenge plus a periodic sweep (covers
  restarts), both running the idempotent :meth:`CaptchaService.expire_due`.
"""

from __future__ import annotations

import contextlib
import contextvars
import html
import logging

from telegram import (
    Bot,
    ChatMember,
    ChatMemberRestricted,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode

from akkiabot_core.observability import log_context, new_trace_id, track_handler
from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.moderation_api import translate_errors
from akkiabot_server.bot.types import App, Context
from akkiabot_server.services.captcha import (
    BAN_RESTRICTIONS,
    MUTE_RESTRICTIONS,
    JoinOutcome,
    StartCheck,
    parse_challenge_id,
)
from akkiabot_server.services.chats import GROUP_TYPES
from akkiabot_server.services.start_payload import StartPayload, start_link

logger = logging.getLogger(__name__)

TIMEOUT_JOB = "captcha_timeout"
SWEEP_JOB = "captcha_timeout_sweep"
SWEEP_INTERVAL = 30
"""Seconds between periodic sweeps (catches challenges whose one-off job was
lost, e.g. across a restart)."""

PROMPT_BUTTON = "👉点击此处开始入群验证👈"
START_BUTTON = "开始验证"
START_TEXT = "👇点击下方按钮完成入群验证👇"
START_WRONG_USER = "❌该验证按钮不属于你，请使用你自己的链接进行验证❌"
START_EXPIRED = "验证已超时。请联系群管理员解除禁言，或退群后重新加入以获取新的验证。"
START_USED = "你已经完成了这次验证。"
START_NOT_FOUND = "验证链接无效，请回到群里重新获取。"
START_UNAVAILABLE = "验证页面暂未配置，请联系群管理员。"


def _is_member(member: ChatMember) -> bool:
    if isinstance(member, ChatMemberRestricted):
        return member.is_member
    return member.status in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER)


def joined(old: ChatMember, new: ChatMember) -> bool:
    """Non-member → member, including rejoining while still restricted."""
    return not _is_member(old) and _is_member(new)


def mute_lifted(old: ChatMember, new: ChatMember) -> bool:
    """Restricted → plain member / admin (an admin lifted the restrictions)."""
    return old.status == ChatMember.RESTRICTED and new.status in (
        ChatMember.MEMBER,
        ChatMember.ADMINISTRATOR,
        ChatMember.OWNER,
    )


def ban_lifted(old: ChatMember, new: ChatMember) -> bool:
    """Banned → anything else (an admin unbanned the user)."""
    return old.status == ChatMember.BANNED and new.status != ChatMember.BANNED


def restricted_by_admin(change: ChatMemberUpdated, bot_id: int) -> bool:
    """Someone other than the bot set restrictions on a member who stays in the
    chat (member → restricted, or restricted → restricted with new rights)."""
    new = change.new_chat_member
    return (
        change.from_user.id != bot_id
        and isinstance(new, ChatMemberRestricted)
        and new.is_member
        and _is_member(change.old_chat_member)
    )


def is_captcha_mute(member: ChatMember) -> bool:
    """Exactly what :meth:`PtbCaptchaApi.mute` applies: every permission off, forever."""
    if not isinstance(member, ChatMemberRestricted) or not member.is_member:
        return False
    if member.until_date is not None and member.until_date.timestamp() > 0:
        return False
    permissions = ChatPermissions.no_permissions().to_dict()
    return not any(getattr(member, name, False) for name in permissions)


def _duration_text(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds % 60 == 0 else f"{seconds} 秒"


def prompt_text(user_id: int, display_name: str, timeout: int) -> str:
    mention = f'<a href="tg://user?id={user_id}">{html.escape(display_name)}</a>'
    return (
        f"你好 {mention} 👋\n"
        f"请在 {_duration_text(timeout)}内点击下方按钮，在私聊中完成入群验证；"
        "超时未验证将保持禁言。"
    )


class PtbCaptchaApi:
    """:class:`~akkiabot_server.services.captcha.CaptchaApi` on top of PTB."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def mute(self, chat_id: int, user_id: int) -> None:
        with translate_errors():
            await self._bot.restrict_chat_member(chat_id, user_id, ChatPermissions.no_permissions())

    async def unmute(self, chat_id: int, user_id: int) -> None:
        with translate_errors():
            await self._bot.restrict_chat_member(
                chat_id, user_id, ChatPermissions.all_permissions()
            )

    async def is_captcha_muted(self, chat_id: int, user_id: int) -> bool:
        with translate_errors():
            return is_captcha_mute(await self._bot.get_chat_member(chat_id, user_id))

    async def send_prompt(
        self, chat_id: int, user_id: int, display_name: str, start_token: str, ttl: int
    ) -> int:
        button = InlineKeyboardButton(
            PROMPT_BUTTON, url=start_link(self._bot.username, start_token)
        )
        with translate_errors():
            message = await self._bot.send_message(
                chat_id,
                prompt_text(user_id, display_name, ttl),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup.from_button(button),
            )
        return message.message_id

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        with translate_errors():
            await self._bot.delete_message(chat_id, message_id)

    async def answer_web_app_query(self, query_id: str, text: str) -> None:
        with translate_errors():
            await self._bot.answer_web_app_query(
                query_id,
                InlineQueryResultArticle(
                    id="captcha_result",
                    title="入群验证",
                    input_message_content=InputTextMessageContent(text),
                ),
            )


async def handle_chat_member(update: Update, context: Context) -> None:
    """Another user's membership changed (needs ``chat_member`` in allowed_updates
    and the bot being an administrator)."""
    change = update.chat_member
    if change is None or change.chat.type not in GROUP_TYPES:
        return
    old, new = change.old_chat_member, change.new_chat_member
    user = new.user
    if user.is_bot:
        return
    deps = get_deps(context)
    if joined(old, new):
        if new.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            return
        result = await deps.captcha.handle_join(
            PtbCaptchaApi(context.bot), change.chat.id, user.id, user.first_name
        )
        logger.info(
            "member joined",
            extra={"target_user_id": user.id, "captcha": result.outcome.value},
        )
        if result.outcome is JoinOutcome.CHALLENGED and context.job_queue is not None:
            # Schedule from an empty contextvars context: APScheduler re-arms its
            # wakeup timer from the caller's context, so scheduling here would
            # tag every later job-run log line with this update's chat_id /
            # user_id / handler (and pollute filtering logs by chat_id).
            contextvars.Context().run(
                context.job_queue.run_once,
                captcha_timeout,
                when=deps.captcha.timeout + 1,
                name=f"{TIMEOUT_JOB}:{result.challenge_id}",
            )
    elif mute_lifted(old, new):
        await deps.captcha.handle_unrestricted(change.chat.id, user.id, cleared=MUTE_RESTRICTIONS)
    elif ban_lifted(old, new):
        await deps.captcha.handle_unrestricted(change.chat.id, user.id, cleared=BAN_RESTRICTIONS)
    elif restricted_by_admin(change, context.bot.id):
        await deps.captcha.handle_restricted(change.chat.id, user.id, by=change.from_user.id)


async def captcha_start(update: Update, context: Context, payload: StartPayload) -> None:
    """``/start <token>`` from a captcha prompt: Mini App button for the right user."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    deps = get_deps(context)
    challenge_id = parse_challenge_id(payload.data.get("challenge_id"))
    check = await deps.captcha.check_start(challenge_id, user.id)
    if check is not StartCheck.OK or challenge_id is None:
        await message.reply_text(
            {
                StartCheck.WRONG_USER: START_WRONG_USER,
                StartCheck.EXPIRED: START_EXPIRED,
                StartCheck.USED: START_USED,
            }.get(check, START_NOT_FOUND)
        )
        return
    base = deps.settings.webapp.url
    if base is None:
        logger.error("WEBAPP_URL is not configured; cannot open the captcha page")
        await message.reply_text(START_UNAVAILABLE)
        return
    url = f"{str(base).rstrip('/')}/captcha?challenge={challenge_id}"
    button = InlineKeyboardButton(START_BUTTON, web_app=WebAppInfo(url))
    await message.reply_text(START_TEXT, reply_markup=InlineKeyboardMarkup.from_button(button))


@track_handler("job:" + TIMEOUT_JOB)
async def _expire(context: Context) -> None:
    await get_deps(context).captcha.expire_due(PtbCaptchaApi(context.bot))


async def captcha_timeout(context: Context) -> None:
    # Failures are counted and logged by track_handler; the sweep retries.
    with log_context(trace_id=new_trace_id()), contextlib.suppress(Exception):
        await _expire(context)


def schedule_captcha_jobs(application: App) -> None:
    job_queue = application.job_queue
    if job_queue is None:  # pragma: no cover - the job-queue extra is a dependency
        raise RuntimeError("python-telegram-bot[job-queue] 未安装")
    job_queue.run_repeating(captcha_timeout, interval=SWEEP_INTERVAL, first=10, name=SWEEP_JOB)
