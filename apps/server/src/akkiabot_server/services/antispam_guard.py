"""Spam protection use cases: group message checks and ``/spam`` reports.

Group messages (text or media caption) go through::

    antispam switch on? → not seen before? → sender is a new member? → LLM
      → spam: delete the message, mute the sender, purge their logged history
        after ``purge_delay`` and post a notice that deletes itself after
        ``notice_ttl``.

Members with more than ``exempt_message_count`` recorded messages are exempt
(botlab-core used 3); administrators are never acted upon. Every verdict is
stored in ``message_log.spam_verdict`` (the text only for hits, for auditing)
and counted in ``antispam_verdict_total{verdict,action}``.

``/spam`` reports by ordinary members are judged by the LLM with the
reported user's name and bio: spam → ban, otherwise all administrators are
mentioned for a manual review.

The classifier fails open (any LLM problem means "not spam"), and Telegram is
reached through :class:`AntiSpamApi`, so the rules are unit-tested without
Telegram or an LLM.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_antispam import SpamContext, SpamVerdict
from akkiabot_core.db import ChatMemberRepository, MessageLogRepository, Restriction, session_scope
from akkiabot_core.observability import (
    count_antispam_verdict,
    count_moderation_action,
    count_telegram_api_error,
    observe_antispam_llm,
)
from akkiabot_core.telegram import escape_markdown_v2, public_name
from akkiabot_server.services.background import BackgroundTasks
from akkiabot_server.services.chat_settings import ChatSettingsService
from akkiabot_server.services.moderation import (
    ModerationApi,
    ModerationApiError,
    ModerationService,
    RateLimitedError,
)

logger = logging.getLogger(__name__)
_e = escape_markdown_v2


class SpamClassifier(Protocol):
    async def classify(self, text: str, *, context: SpamContext | None = None) -> SpamVerdict: ...


@dataclass(frozen=True, slots=True)
class AdminInfo:
    user_id: int
    name: str
    is_bot: bool = False


class AntiSpamApi(ModerationApi, Protocol):
    """Telegram operations used here (implemented by the bot layer).

    Like :class:`ModerationApi`, failures raise :class:`ModerationApiError` or
    :class:`RateLimitedError`. Texts are MarkdownV2.
    """

    async def mute(self, chat_id: int, user_id: int) -> None:
        """Restrict the member to no rights, permanently."""
        ...

    async def send_message(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        """Send a message and return its id."""
        ...

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None: ...

    async def get_bio(self, user_id: int) -> str | None: ...

    async def list_admins(self, chat_id: int) -> Sequence[AdminInfo]: ...


def mention(name: str, user_id: int) -> str:
    """MarkdownV2 mention link showing the full name (administrators only)."""
    return f"[{_e(name or str(user_id))}](tg://user?id={user_id})"


def target_mention(name: str, user_id: int) -> str:
    """MarkdownV2 mention of the handled user: only a fragment of the name is shown."""
    return f"[{_e(public_name(name))}](tg://user?id={user_id})"


@dataclass(frozen=True, slots=True)
class GroupMessage:
    """The facts about a group message the guard needs."""

    chat_id: int
    message_id: int
    sender_id: int
    sender_name: str
    text: str


class GuardAction(StrEnum):
    """What the guard did; also the ``action`` label of ``antispam_verdict_total``."""

    DISABLED = "disabled"
    NO_TEXT = "no_text"
    DUPLICATE = "duplicate"
    UNAVAILABLE = "unavailable"
    EXEMPT = "exempt"
    NONE = "none"
    ADMIN = "admin"
    REMOVED = "removed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    action: GuardAction
    verdict: SpamVerdict | None = None
    deleted: bool = False
    muted: bool = False


@dataclass(frozen=True, slots=True)
class SpamReport:
    chat_id: int
    reporter_id: int
    target_id: int
    target_name: str
    message_id: int
    text: str
    status_message_id: int
    """The bot's "report received" message, edited with the result."""
    bot_id: int


class ReportAction(StrEnum):
    BANNED = "report_ban"
    ESCALATED = "report_escalate"
    BAN_FAILED = "report_ban_failed"


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    action: ReportAction
    verdict: SpamVerdict | None


def _verdict_label(verdict: SpamVerdict | None) -> str:
    if verdict is None:
        return "skipped"
    if verdict.failed:
        return "error"
    return "spam" if verdict.is_spam else "ham"


type Sleep = Callable[[float], Awaitable[None]]


class AntiSpamGuard:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_settings: ChatSettingsService,
        moderation: ModerationService,
        background: BackgroundTasks,
        classifier: SpamClassifier | None,
        *,
        exempt_message_count: int = 3,
        purge_delay: float = 5.0,
        notice_ttl: float = 60.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._chat_settings = chat_settings
        self._moderation = moderation
        self._background = background
        self.classifier = classifier
        self.exempt_message_count = exempt_message_count
        self._purge_delay = purge_delay
        self._notice_ttl = notice_ttl
        self.sleep = sleep

    @property
    def available(self) -> bool:
        """Whether an LLM is configured."""
        return self.classifier is not None

    async def classify(self, text: str, context: SpamContext | None = None) -> SpamVerdict | None:
        """Classify with metrics; ``None`` when no LLM is configured."""
        if self.classifier is None:
            return None
        start = time.perf_counter()
        verdict = await self.classifier.classify(text, context=context)
        observe_antispam_llm(time.perf_counter() - start, error=verdict.error)
        return verdict

    # ---------------------------------------------------------- group messages
    async def check(self, api: AntiSpamApi, message: GroupMessage) -> GuardOutcome:
        outcome = await self._check(api, message)
        if outcome.action not in (
            GuardAction.DISABLED,
            GuardAction.NO_TEXT,
            GuardAction.DUPLICATE,
        ):
            count_antispam_verdict(_verdict_label(outcome.verdict), outcome.action.value)
        log = logger.info if outcome.verdict and outcome.verdict.is_spam else logger.debug
        log(
            "antispam check",
            extra={
                "message_id": message.message_id,
                "action": outcome.action.value,
                "verdict": _verdict_label(outcome.verdict),
                "category": outcome.verdict.category if outcome.verdict else None,
                "prompt_version": outcome.verdict.prompt_version if outcome.verdict else None,
            },
        )
        return outcome

    async def _check(self, api: AntiSpamApi, message: GroupMessage) -> GuardOutcome:
        if not message.text.strip():
            return GuardOutcome(GuardAction.NO_TEXT)
        if not (await self._chat_settings.get(message.chat_id)).antispam:
            return GuardOutcome(GuardAction.DISABLED)
        async with session_scope(self._session_factory) as session:
            logged = await MessageLogRepository(session).get(message.chat_id, message.message_id)
            member = await ChatMemberRepository(session).get(message.chat_id, message.sender_id)
        if logged is not None and logged.spam_verdict is not None:
            return GuardOutcome(GuardAction.DUPLICATE)  # redelivered update
        # The bookkeeping handler already counted this message.
        if member is not None and member.message_count > self.exempt_message_count:
            return GuardOutcome(GuardAction.EXEMPT)
        if self.classifier is None:
            return GuardOutcome(GuardAction.UNAVAILABLE)

        verdict = await self.classify(message.text)
        assert verdict is not None  # noqa: S101 - classifier is configured
        if not verdict.is_spam:
            await self._record(message.chat_id, message.message_id, verdict, text=None)
            return GuardOutcome(GuardAction.NONE, verdict)
        # Keep the evidence before anything is deleted.
        await self._record(message.chat_id, message.message_id, verdict, text=message.text)
        try:
            sender = await api.get_member(message.chat_id, message.sender_id)
        except (ModerationApiError, RateLimitedError):
            count_telegram_api_error("getChatMember")
            logger.warning("antispam: member lookup failed, not acting", exc_info=True)
            return GuardOutcome(GuardAction.FAILED, verdict)
        if sender.is_admin:
            return GuardOutcome(GuardAction.ADMIN, verdict)
        return await self._remove(api, message, verdict)

    async def _remove(
        self, api: AntiSpamApi, message: GroupMessage, verdict: SpamVerdict
    ) -> GuardOutcome:
        deleted = await self._try(
            "deleteMessages", api.delete_messages(message.chat_id, [message.message_id])
        )
        if deleted:
            count_moderation_action("delete")
        muted = await self._try("restrictChatMember", api.mute(message.chat_id, message.sender_id))
        if muted:
            async with session_scope(self._session_factory) as session:
                await ChatMemberRepository(session).set_restriction(
                    message.chat_id, message.sender_id, Restriction.SPAM_MUTE
                )
            count_moderation_action("mute")
        self._background.spawn(
            self._purge_later(api, message.chat_id, message.sender_id),
            name=f"antispam:purge:{message.chat_id}:{message.sender_id}",
        )
        await self._notify(api, message, deleted=deleted, muted=muted)
        action = GuardAction.REMOVED if deleted or muted else GuardAction.FAILED
        return GuardOutcome(action, verdict, deleted=deleted, muted=muted)

    async def _notify(
        self, api: AntiSpamApi, message: GroupMessage, *, deleted: bool, muted: bool
    ) -> None:
        actions = [
            "删除消息" if deleted else "删除消息失败",
            "禁言" if muted else "禁言失败",
        ]
        text = (
            _e("[Anti SPAM]\n内容命中反垃圾策略\n用户：")
            + target_mention(message.sender_name, message.sender_id)
            + _e(f"\n动作：{'、'.join(actions)}")
        )
        if not (deleted and muted):
            text += _e("\n请确认机器人具有删除消息和封禁成员权限。")
        try:
            notice_id = await api.send_message(message.chat_id, text)
        except (ModerationApiError, RateLimitedError):
            count_telegram_api_error("sendMessage")
            logger.warning("antispam: failed to send the notice", exc_info=True)
            return
        self._background.spawn(
            self._delete_later(api, message.chat_id, notice_id),
            name=f"antispam:notice:{message.chat_id}:{notice_id}",
        )

    async def _purge_later(self, api: AntiSpamApi, chat_id: int, user_id: int) -> None:
        # Give Telegram a moment: messages sent right before the hit are logged too.
        await self.sleep(self._purge_delay)
        await self._moderation.purge_history(api, chat_id, user_id)

    async def _delete_later(self, api: AntiSpamApi, chat_id: int, message_id: int) -> None:
        await self.sleep(self._notice_ttl)
        await self._try("deleteMessages", api.delete_messages(chat_id, [message_id]))

    async def _record(
        self, chat_id: int, message_id: int, verdict: SpamVerdict, *, text: str | None
    ) -> None:
        data = verdict.model_dump(mode="json")
        async with session_scope(self._session_factory) as session:
            repo = MessageLogRepository(session)
            if text is None:
                await repo.record_verdict(chat_id, message_id, verdict=data)
            else:
                await repo.mark_spam(chat_id, message_id, verdict=data, text=text)

    @staticmethod
    async def _try(method: str, call: Awaitable[None]) -> bool:
        try:
            await call
        except (ModerationApiError, RateLimitedError):
            count_telegram_api_error(method)
            logger.warning(
                "antispam: telegram call failed", extra={"method": method}, exc_info=True
            )
            return False
        return True

    # ------------------------------------------------------------ /spam reports
    async def review_report(self, api: AntiSpamApi, report: SpamReport) -> ReportOutcome:
        """Judge a member's report; ban on spam, otherwise call the administrators."""
        bio: str | None = None
        try:
            bio = await api.get_bio(report.target_id)
        except Exception:  # best effort: judge without the bio
            count_telegram_api_error("getChat")
            logger.warning("antispam: could not read the reported user's bio", exc_info=True)
        verdict = await self.classify(
            report.text, SpamContext(sender_name=report.target_name, sender_bio=bio)
        )
        if verdict is not None and verdict.is_spam:
            outcome = await self._ban_reported(api, report, verdict)
        else:
            outcome = await self._escalate(api, report, verdict)
        count_antispam_verdict(_verdict_label(verdict), outcome.action.value)
        logger.info(
            "spam report reviewed",
            extra={
                "target_user_id": report.target_id,
                "action": outcome.action.value,
                "verdict": _verdict_label(verdict),
            },
        )
        return outcome

    async def _ban_reported(
        self, api: AntiSpamApi, report: SpamReport, verdict: SpamVerdict
    ) -> ReportOutcome:
        await self._record(report.chat_id, report.message_id, verdict, text=report.text)
        target = target_mention(report.target_name, report.target_id)
        try:
            result = await self._moderation.ban(
                api,
                report.chat_id,
                report.target_id,
                by=report.bot_id,
                restriction=Restriction.ADMIN_BAN,
                extra_message_ids=[report.message_id],
            )
        except (ModerationApiError, RateLimitedError):
            logger.warning("antispam: ban after report failed", exc_info=True)
            await self._edit(
                api,
                report,
                _e("举报成立：判定为垃圾信息，但封禁 ")
                + target
                + _e(" 失败，请确认机器人具有封禁成员权限。"),
            )
            return ReportOutcome(ReportAction.BAN_FAILED, verdict)
        await self._edit(
            api,
            report,
            _e("举报成立：判定为垃圾信息，已封禁 ")
            + target
            + _e(f"，删除了 {result.purge.deleted} 条消息。"),
        )
        return ReportOutcome(ReportAction.BANNED, verdict)

    async def _escalate(
        self, api: AntiSpamApi, report: SpamReport, verdict: SpamVerdict | None
    ) -> ReportOutcome:
        if verdict is not None and not verdict.failed:
            await self._record(report.chat_id, report.message_id, verdict, text=None)
        try:
            admins = [a for a in await api.list_admins(report.chat_id) if not a.is_bot]
        except (ModerationApiError, RateLimitedError):
            count_telegram_api_error("getChatAdministrators")
            logger.warning("antispam: could not list administrators", exc_info=True)
            admins = []
        automatic = verdict is not None and not verdict.failed
        reason = "未判定为垃圾信息" if automatic else "暂时无法自动审核"
        text = _e(f"{reason}，请管理员人工审核被举报的消息。")
        if admins:
            text += "\n" + " ".join(mention(a.name, a.user_id) for a in admins)
        await self._edit(api, report, text)
        return ReportOutcome(ReportAction.ESCALATED, verdict)

    async def _edit(self, api: AntiSpamApi, report: SpamReport, text: str) -> None:
        await self._try(
            "editMessageText", api.edit_message(report.chat_id, report.status_message_id, text)
        )
