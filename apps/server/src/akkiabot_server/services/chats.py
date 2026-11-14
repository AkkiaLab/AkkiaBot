"""Chat bookkeeping: remember every group the bot sees, read its settings."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.db import ChatRepository, ChatSettings, session_scope

logger = logging.getLogger(__name__)

GROUP_TYPES = frozenset({"group", "supergroup"})


class ChatDirectory:
    """Writes a ``chat`` row the first time a group is seen.

    Chat ids already known to this process skip the database entirely.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._known: set[int] = set()

    async def ensure(
        self, session: AsyncSession, chat_id: int, *, type: str, title: str | None
    ) -> None:
        if chat_id in self._known:
            return
        repo = ChatRepository(session)
        if await repo.get(chat_id) is None:
            await repo.upsert(chat_id, type=type, title=title)
            logger.info("new chat recorded", extra={"chat_type": type})
        self._known.add(chat_id)

    async def refresh(self, chat_id: int, *, type: str, title: str | None) -> None:
        """Insert or update type/title (bot membership changed, title changed…)."""
        async with session_scope(self._session_factory) as session:
            await ChatRepository(session).upsert(chat_id, type=type, title=title)
        self._known.add(chat_id)

    async def settings(self, chat_id: int) -> ChatSettings:
        async with session_scope(self._session_factory) as session:
            return await ChatRepository(session).get_settings(chat_id)
