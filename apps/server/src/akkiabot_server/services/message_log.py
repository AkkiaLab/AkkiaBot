"""Group message bookkeeping (``message_log`` ids only, never the text)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.db import ChatMemberRepository, MessageLogRepository, session_scope
from akkiabot_core.db.types import utcnow
from akkiabot_server.services.chats import GROUP_TYPES, ChatDirectory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """The framework-independent facts about a group message."""

    chat_id: int
    chat_type: str
    chat_title: str | None
    message_id: int
    sender_id: int | None
    sender_is_bot: bool
    is_service: bool
    date: datetime


def should_record(message: IncomingMessage) -> bool:
    """Record user messages in groups; skip private chats, bots and service messages."""
    return (
        message.chat_type in GROUP_TYPES
        and message.sender_id is not None
        and not message.sender_is_bot
        and not message.is_service
    )


class MessageRecorder:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], chats: ChatDirectory
    ) -> None:
        self._session_factory = session_factory
        self._chats = chats

    async def record(self, message: IncomingMessage) -> bool:
        """Store the message id and bump the sender's counters; returns whether
        anything was recorded."""
        if message.chat_type not in GROUP_TYPES:
            return False
        recorded = should_record(message)
        async with session_scope(self._session_factory) as session:
            await self._chats.ensure(
                session, message.chat_id, type=message.chat_type, title=message.chat_title
            )
            if recorded:
                assert message.sender_id is not None  # noqa: S101 - checked by should_record
                await MessageLogRepository(session).add(
                    message.chat_id,
                    message.message_id,
                    user_id=message.sender_id,
                    created_at=message.date,
                )
                await ChatMemberRepository(session).record_message(
                    message.chat_id, message.sender_id, at=message.date
                )
        # Only after the commit succeeded: a rollback must not hide the chat.
        self._chats.remember(message.chat_id)
        return recorded

    async def cleanup(self, retention: timedelta, *, now: datetime | None = None) -> int:
        """Delete ``message_log`` rows older than ``retention``."""
        cutoff = (now or utcnow()) - retention
        async with session_scope(self._session_factory) as session:
            removed = await MessageLogRepository(session).delete_older_than(cutoff)
        logger.info(
            "message_log retention cleanup",
            extra={"removed": removed, "cutoff": cutoff.isoformat()},
        )
        return removed
