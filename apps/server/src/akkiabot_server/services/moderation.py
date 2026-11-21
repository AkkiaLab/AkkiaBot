"""Moderation use cases: permission rules, ban + history purge, warnings.

The service is framework independent: Telegram is reached through the small
:class:`ModerationApi` protocol (the bot layer adapts PTB to it), so the rules
here are unit-tested without Telegram.

Permission rules:

* an *actor* may moderate when they are the group owner, or an administrator
  with the "ban users" right (``can_restrict_members``);
* a *target* that is the owner or any administrator is protected.

Every action updates ``chat_member.restriction`` and is counted in
``moderation_actions_total``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.db import ChatMemberRepository, MessageLogRepository, Restriction, session_scope
from akkiabot_core.observability import count_moderation_action, count_telegram_api_error
from akkiabot_server.services.chat_settings import ChatSettingsService

logger = logging.getLogger(__name__)

#: ``deleteMessages`` accepts at most 100 ids per call.
DELETE_BATCH_SIZE = 100


class MemberRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    RESTRICTED = "restricted"
    LEFT = "left"
    BANNED = "banned"


@dataclass(frozen=True, slots=True)
class MemberInfo:
    role: MemberRole
    can_restrict_members: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role in (MemberRole.OWNER, MemberRole.ADMIN)

    @property
    def can_moderate(self) -> bool:
        """Owner, or an administrator allowed to restrict (ban) members."""
        return self.role is MemberRole.OWNER or (
            self.role is MemberRole.ADMIN and self.can_restrict_members
        )


class Authorization(StrEnum):
    ALLOWED = "allowed"
    ACTOR_FORBIDDEN = "forbidden"
    TARGET_PROTECTED = "protected"


def authorize(actor: MemberInfo, target: MemberInfo) -> Authorization:
    """May ``actor`` ban / warn ``target``? The actor is checked first."""
    if not actor.can_moderate:
        return Authorization.ACTOR_FORBIDDEN
    if target.is_admin:
        return Authorization.TARGET_PROTECTED
    return Authorization.ALLOWED


class RateLimitedError(Exception):
    """Telegram asked us to wait ``retry_after`` seconds (HTTP 429)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class ModerationApiError(Exception):
    """A Bot API call failed for another reason (no rights, user not found…)."""


class ModerationApi(Protocol):
    """The Telegram operations moderation needs (implemented by the bot layer).

    Implementations raise :class:`RateLimitedError` or :class:`ModerationApiError`.
    """

    async def get_member(self, chat_id: int, user_id: int) -> MemberInfo: ...

    async def ban(self, chat_id: int, user_id: int) -> None: ...

    async def delete_messages(self, chat_id: int, message_ids: Sequence[int]) -> None:
        """Delete up to :data:`DELETE_BATCH_SIZE` messages (missing ones are skipped)."""
        ...


@dataclass(frozen=True, slots=True)
class PurgeResult:
    deleted: int
    failed: int


@dataclass(frozen=True, slots=True)
class BanResult:
    purge: PurgeResult


@dataclass(frozen=True, slots=True)
class WarnResult:
    count: int
    """Warnings including this one (equals ``limit`` when the member got banned)."""
    limit: int
    banned: bool
    purge: PurgeResult | None = None


class WarnDisabledError(Exception):
    """``/warn`` is used in a group that did not enable warnings."""


type Sleep = Callable[[float], Awaitable[None]]


class ModerationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_settings: ChatSettingsService,
        *,
        delete_concurrency: int = 4,
        max_retries: int = 3,
        max_retry_after: float = 60.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if delete_concurrency <= 0 or max_retries < 0:
            raise ValueError("delete_concurrency 必须为正数，max_retries 不能为负数")
        self._session_factory = session_factory
        self._chat_settings = chat_settings
        self._delete_concurrency = delete_concurrency
        self._max_retries = max_retries
        self._max_retry_after = max_retry_after
        self._sleep = sleep

    async def ban(
        self,
        api: ModerationApi,
        chat_id: int,
        user_id: int,
        *,
        by: int,
        restriction: Restriction = Restriction.ADMIN_BAN,
        extra_message_ids: Iterable[int] = (),
    ) -> BanResult:
        """Ban ``user_id`` and delete their logged messages (plus ``extra_message_ids``).

        Permission checks are the caller's job (see :func:`authorize`). Raises
        :class:`ModerationApiError` / :class:`RateLimitedError` when the ban itself
        fails; nothing is recorded then.
        """
        await self._call(api.ban, chat_id, user_id, method="banChatMember")
        async with session_scope(self._session_factory) as session:
            await ChatMemberRepository(session).set_restriction(
                chat_id, user_id, restriction, restricted_by=by
            )
        count_moderation_action("ban")
        logger.info(
            "member banned",
            extra={"target_user_id": user_id, "restriction": restriction.value},
        )
        purge = await self.purge_history(api, chat_id, user_id, extra_message_ids)
        return BanResult(purge)

    async def warn(self, api: ModerationApi, chat_id: int, user_id: int, *, by: int) -> WarnResult:
        """Add a warning; at ``max_warn_count`` ban the member and reset the count.

        If the ban fails the count stays at the limit, so the next ``/warn``
        tries again; the error propagates.
        """
        settings = await self._chat_settings.get(chat_id)
        if not settings.warn:
            raise WarnDisabledError(chat_id)
        limit = settings.max_warn_count
        async with session_scope(self._session_factory) as session:
            count = await ChatMemberRepository(session).add_warning(chat_id, user_id)
        count_moderation_action("warn")
        logger.info(
            "member warned", extra={"target_user_id": user_id, "warn_count": count, "limit": limit}
        )
        if count < limit:
            return WarnResult(count, limit, banned=False)

        await self._call(api.ban, chat_id, user_id, method="banChatMember")
        async with session_scope(self._session_factory) as session:
            members = ChatMemberRepository(session)
            await members.reset_warnings(chat_id, user_id)
            await members.set_restriction(chat_id, user_id, Restriction.WARN_BAN, restricted_by=by)
        count_moderation_action("ban")
        logger.info(
            "member banned after reaching the warning limit", extra={"target_user_id": user_id}
        )
        purge = await self.purge_history(api, chat_id, user_id)
        return WarnResult(limit, limit, banned=True, purge=purge)

    async def purge_history(
        self, api: ModerationApi, chat_id: int, user_id: int, extra_message_ids: Iterable[int] = ()
    ) -> PurgeResult:
        """Delete the member's logged messages in batches, at most
        ``delete_concurrency`` requests in flight; rows of deleted batches are
        removed from ``message_log``. Failures are counted, never raised."""
        async with session_scope(self._session_factory) as session:
            logged = await MessageLogRepository(session).list_message_ids(chat_id, user_id)
        ids = sorted(set(logged) | set(extra_message_ids))
        batches = [ids[i : i + DELETE_BATCH_SIZE] for i in range(0, len(ids), DELETE_BATCH_SIZE)]
        semaphore = asyncio.Semaphore(self._delete_concurrency)

        async def run(batch: list[int]) -> bool:
            async with semaphore:
                try:
                    await self._call(api.delete_messages, chat_id, batch, method="deleteMessages")
                except (ModerationApiError, RateLimitedError):
                    logger.warning(
                        "failed to delete a batch of messages",
                        extra={"target_user_id": user_id, "batch_size": len(batch)},
                        exc_info=True,
                    )
                    return False
            return True

        outcomes = await asyncio.gather(*(run(batch) for batch in batches))
        done = [mid for batch, ok in zip(batches, outcomes, strict=True) if ok for mid in batch]
        failed = len(ids) - len(done)
        if done:
            async with session_scope(self._session_factory) as session:
                await MessageLogRepository(session).delete_messages(chat_id, done)
        count_moderation_action("delete", len(done))
        logger.info(
            "member history purged",
            extra={"target_user_id": user_id, "deleted": len(done), "failed": failed},
        )
        return PurgeResult(deleted=len(done), failed=failed)

    async def _call[*A](self, fn: Callable[[*A], Awaitable[None]], *args: *A, method: str) -> None:
        """Call ``fn``; on rate limits wait as told and retry (``max_retries`` times)."""
        attempt = 0
        while True:
            try:
                await fn(*args)
                return
            except RateLimitedError as exc:
                count_telegram_api_error(method)
                attempt += 1
                if attempt > self._max_retries or exc.retry_after > self._max_retry_after:
                    raise
                logger.warning(
                    "telegram rate limit, backing off",
                    extra={"method": method, "retry_after": exc.retry_after, "attempt": attempt},
                )
                await self._sleep(exc.retry_after)
            except ModerationApiError:
                count_telegram_api_error(method)
                raise
