"""Repositories: the only place that issues SQL (docs/architecture.md §5.1).

Repositories take an :class:`AsyncSession` and never commit; the caller owns
the transaction (see :func:`akkiabot_core.db.session_scope`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from akkiabot_core.db.models import (
    CaptchaChallenge,
    CaptchaStatus,
    Chat,
    ChatMember,
    ChatSettings,
    MessageLog,
    Restriction,
)
from akkiabot_core.db.types import utcnow


def _insert(session: AsyncSession, model: type[Any]) -> postgresql.Insert | sqlite.Insert:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.insert(model)
    if dialect == "sqlite":
        return sqlite.insert(model)
    raise NotImplementedError(f"unsupported dialect: {dialect}")


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, chat_id: int) -> Chat | None:
        return await self.session.get(Chat, chat_id)

    async def upsert(self, chat_id: int, *, type: str, title: str | None) -> Chat:
        """Insert the chat or refresh its type/title; settings are kept."""
        now = utcnow()
        stmt = _insert(self.session, Chat).values(
            id=chat_id, type=type, title=title, settings={}, created_at=now, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[Chat.id],
            set_={"type": type, "title": title, "updated_at": now},
        )
        await self.session.execute(stmt)
        chat = await self.session.get(Chat, chat_id, populate_existing=True)
        assert chat is not None  # noqa: S101
        return chat

    async def get_settings(self, chat_id: int) -> ChatSettings:
        chat = await self.get(chat_id)
        return chat.chat_settings if chat else ChatSettings()

    async def update_settings(self, chat_id: int, settings: ChatSettings) -> None:
        await self.session.execute(
            update(Chat)
            .where(Chat.id == chat_id)
            .values(settings=settings.model_dump(mode="json"), updated_at=utcnow())
        )


class ChatMemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, chat_id: int, user_id: int) -> ChatMember | None:
        return await self.session.get(ChatMember, (chat_id, user_id))

    async def get_or_create(
        self, chat_id: int, user_id: int, *, joined_at: datetime | None = None
    ) -> ChatMember:
        stmt = (
            _insert(self.session, ChatMember)
            .values(chat_id=chat_id, user_id=user_id, joined_at=joined_at)
            .on_conflict_do_nothing(index_elements=[ChatMember.chat_id, ChatMember.user_id])
        )
        await self.session.execute(stmt)
        member = await self.session.get(ChatMember, (chat_id, user_id))
        assert member is not None  # noqa: S101
        return member

    async def record_message(
        self, chat_id: int, user_id: int, *, at: datetime | None = None
    ) -> None:
        """Increment ``message_count`` and set ``last_message_at`` (upsert)."""
        at = at or utcnow()
        stmt = _insert(self.session, ChatMember).values(
            chat_id=chat_id, user_id=user_id, message_count=1, last_message_at=at
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChatMember.chat_id, ChatMember.user_id],
            set_={
                "message_count": ChatMember.message_count + 1,
                "last_message_at": at,
            },
        )
        await self.session.execute(stmt)

    async def set_restriction(
        self,
        chat_id: int,
        user_id: int,
        restriction: Restriction,
        *,
        restricted_by: int | None = None,
        until: datetime | None = None,
    ) -> None:
        await self.get_or_create(chat_id, user_id)
        await self.session.execute(
            update(ChatMember)
            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
            .values(restriction=restriction, restricted_by=restricted_by, restricted_until=until)
        )

    async def add_warning(self, chat_id: int, user_id: int) -> int:
        """Increment ``warn_count`` and return the new value."""
        await self.get_or_create(chat_id, user_id)
        result = await self.session.execute(
            update(ChatMember)
            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
            .values(warn_count=ChatMember.warn_count + 1)
            .returning(ChatMember.warn_count)
        )
        return result.scalar_one()

    async def reset_warnings(self, chat_id: int, user_id: int) -> None:
        await self.session.execute(
            update(ChatMember)
            .where(ChatMember.chat_id == chat_id, ChatMember.user_id == user_id)
            .values(warn_count=0)
        )


class MessageLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        chat_id: int,
        message_id: int,
        *,
        user_id: int | None,
        created_at: datetime | None = None,
    ) -> bool:
        """Record a message id (no text). Duplicates are ignored.

        Returns ``True`` only when a new row was inserted, so callers can keep
        derived counters idempotent under redelivery.
        """
        stmt = (
            _insert(self.session, MessageLog)
            .values(
                chat_id=chat_id,
                message_id=message_id,
                user_id=user_id,
                created_at=created_at or utcnow(),
            )
            .on_conflict_do_nothing(index_elements=[MessageLog.chat_id, MessageLog.message_id])
        )
        result = await self.session.execute(stmt)
        assert isinstance(result, CursorResult)  # noqa: S101
        return result.rowcount == 1

    async def get(self, chat_id: int, message_id: int) -> MessageLog | None:
        return await self.session.get(MessageLog, (chat_id, message_id))

    async def mark_spam(
        self, chat_id: int, message_id: int, *, verdict: dict[str, Any], text: str | None
    ) -> None:
        """Attach the spam verdict, and keep the text for auditing."""
        await self.session.execute(
            update(MessageLog)
            .where(MessageLog.chat_id == chat_id, MessageLog.message_id == message_id)
            .values(spam_verdict=verdict, text=text)
        )

    async def list_message_ids(self, chat_id: int, user_id: int) -> Sequence[int]:
        result = await self.session.scalars(
            select(MessageLog.message_id)
            .where(MessageLog.chat_id == chat_id, MessageLog.user_id == user_id)
            .order_by(MessageLog.message_id)
        )
        return result.all()

    async def delete_messages(self, chat_id: int, message_ids: Sequence[int]) -> int:
        if not message_ids:
            return 0
        result = await self.session.execute(
            delete(MessageLog).where(
                MessageLog.chat_id == chat_id, MessageLog.message_id.in_(message_ids)
            )
        )
        assert isinstance(result, CursorResult)  # noqa: S101
        return result.rowcount

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Retention cleanup; returns the number of rows removed."""
        result = await self.session.execute(
            delete(MessageLog).where(MessageLog.created_at < cutoff)
        )
        assert isinstance(result, CursorResult)  # noqa: S101
        return result.rowcount


class CaptchaChallengeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        chat_id: int,
        user_id: int,
        *,
        ttl: timedelta,
        group_message_id: int | None = None,
    ) -> CaptchaChallenge:
        now = utcnow()
        challenge = CaptchaChallenge(
            id=uuid.uuid4(),
            chat_id=chat_id,
            user_id=user_id,
            status=CaptchaStatus.PENDING,
            created_at=now,
            expires_at=now + ttl,
            group_message_id=group_message_id,
        )
        self.session.add(challenge)
        await self.session.flush()
        return challenge

    async def get(self, challenge_id: uuid.UUID) -> CaptchaChallenge | None:
        return await self.session.get(CaptchaChallenge, challenge_id)

    async def get_pending(self, chat_id: int, user_id: int) -> CaptchaChallenge | None:
        return await self.session.scalar(
            select(CaptchaChallenge)
            .where(
                CaptchaChallenge.chat_id == chat_id,
                CaptchaChallenge.user_id == user_id,
                CaptchaChallenge.status == CaptchaStatus.PENDING,
            )
            .order_by(CaptchaChallenge.created_at.desc())
            .limit(1)
        )

    async def list_pending(self, chat_id: int, user_id: int) -> Sequence[CaptchaChallenge]:
        """Every pending challenge of the member, oldest first."""
        result = await self.session.scalars(
            select(CaptchaChallenge)
            .where(
                CaptchaChallenge.chat_id == chat_id,
                CaptchaChallenge.user_id == user_id,
                CaptchaChallenge.status == CaptchaStatus.PENDING,
            )
            .order_by(CaptchaChallenge.created_at)
        )
        return result.all()

    async def set_group_message(self, challenge_id: uuid.UUID, message_id: int) -> None:
        """Remember the group prompt so it can be deleted once the challenge ends."""
        await self.session.execute(
            update(CaptchaChallenge)
            .where(CaptchaChallenge.id == challenge_id)
            .values(group_message_id=message_id)
        )

    async def reopen(self, challenge_id: uuid.UUID, *, now: datetime | None = None) -> bool:
        """Undo a ``PASSED`` claim (the follow-up Telegram call failed) so the
        user can retry. Only while the challenge has not expired."""
        result = await self.session.execute(
            update(CaptchaChallenge)
            .where(
                CaptchaChallenge.id == challenge_id,
                CaptchaChallenge.status == CaptchaStatus.PASSED,
                CaptchaChallenge.expires_at > (now or utcnow()),
            )
            .values(status=CaptchaStatus.PENDING)
        )
        assert isinstance(result, CursorResult)  # noqa: S101
        return result.rowcount == 1

    async def resolve(
        self, challenge_id: uuid.UUID, status: CaptchaStatus, *, now: datetime | None = None
    ) -> bool:
        """Move a *pending* challenge to ``status`` (compare-and-set).

        Returns ``False`` if it was already resolved, so a challenge can only be
        passed once even under concurrent requests. Only ``EXPIRED`` may be set
        once ``expires_at`` is reached; ``PASSED`` / ``FAILED`` on an overdue
        challenge are rejected even before the expiry job has run.
        """
        if status is CaptchaStatus.PENDING:
            raise ValueError("cannot resolve a challenge to PENDING")
        conditions = [
            CaptchaChallenge.id == challenge_id,
            CaptchaChallenge.status == CaptchaStatus.PENDING,
        ]
        if status is not CaptchaStatus.EXPIRED:
            conditions.append(CaptchaChallenge.expires_at > (now or utcnow()))
        result = await self.session.execute(
            update(CaptchaChallenge).where(*conditions).values(status=status)
        )
        assert isinstance(result, CursorResult)  # noqa: S101
        return result.rowcount == 1

    async def expire_due(self, now: datetime | None = None) -> Sequence[CaptchaChallenge]:
        """Mark pending challenges past ``expires_at`` as expired and return them."""
        now = now or utcnow()
        due = (
            await self.session.scalars(
                select(CaptchaChallenge).where(
                    CaptchaChallenge.status == CaptchaStatus.PENDING,
                    CaptchaChallenge.expires_at <= now,
                )
            )
        ).all()
        expired = [c for c in due if await self.resolve(c.id, CaptchaStatus.EXPIRED)]
        for challenge in expired:
            await self.session.refresh(challenge)
        return expired
