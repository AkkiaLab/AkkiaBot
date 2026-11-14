"""Per-group feature switches (``chat.settings``): read through a cache, validated writes.

Settings live in the portable JSON column ``chat.settings`` and are validated by
:class:`~akkiabot_core.db.ChatSettings` on every read and write. Reads go through
the shared :class:`~akkiabot_core.cache.Cache` (short TTL); every write deletes
the cached entry after its transaction committed, so the next read sees it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.cache import Cache
from akkiabot_core.db import ChatRepository, ChatSettings, session_scope

logger = logging.getLogger(__name__)

KEY_PREFIX = "chat_settings:"
DEFAULT_CACHE_TTL = 300


class Toggle(StrEnum):
    """Switches offered by ``/config`` (values are ``ChatSettings`` field names)."""

    ANTISPAM = "antispam"
    CAPTCHA = "captcha"
    WARN = "warn"
    DELETE_SERVICE_MESSAGES = "delete_service_messages"


TOGGLE_LABELS: dict[Toggle, str] = {
    Toggle.ANTISPAM: "反垃圾",
    Toggle.CAPTCHA: "入群验证",
    Toggle.WARN: "警告",
    Toggle.DELETE_SERVICE_MESSAGES: "删除系统消息",
}


class UnknownChatError(LookupError):
    """The chat has no ``chat`` row yet (the bot never saw it)."""


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """What the ``/config`` menu shows for one group."""

    chat_id: int
    title: str | None
    settings: ChatSettings


def _key(chat_id: int) -> str:
    return f"{KEY_PREFIX}{chat_id}"


class ChatSettingsService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cache: Cache,
        *,
        cache_ttl: int = DEFAULT_CACHE_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._cache = cache
        self._cache_ttl = cache_ttl

    async def get(self, chat_id: int) -> ChatSettings:
        """Settings of ``chat_id`` (defaults for unknown chats)."""
        cached = await self._cache_get(chat_id)
        if cached is not None:
            return cached
        async with session_scope(self._session_factory) as session:
            settings = await ChatRepository(session).get_settings(chat_id)
        await self._cache_set(chat_id, settings)
        return settings

    async def describe(self, chat_id: int) -> ChatConfig | None:
        """Title and settings straight from the database; ``None`` for unknown chats."""
        async with session_scope(self._session_factory) as session:
            chat = await ChatRepository(session).get(chat_id)
            if chat is None:
                return None
            return ChatConfig(chat.id, chat.title, chat.chat_settings)

    async def update(self, chat_id: int, **changes: Any) -> ChatSettings:
        """Apply ``changes`` (validated as a whole) and invalidate the cache.

        Raises :class:`UnknownChatError` for chats without a row and
        :class:`pydantic.ValidationError` for invalid values.
        """
        async with session_scope(self._session_factory) as session:
            repo = ChatRepository(session)
            chat = await repo.get(chat_id)
            if chat is None:
                raise UnknownChatError(chat_id)
            current = chat.chat_settings.model_dump()
            unknown = set(changes) - set(current)
            if unknown:
                raise ValueError(f"未知的群配置项：{', '.join(sorted(unknown))}")
            settings = ChatSettings.model_validate(current | changes)
            await repo.update_settings(chat_id, settings)
        # After the commit: a reader racing us may have cached the old value just
        # before this delete; the TTL bounds how long that can last.
        await self._cache_delete(chat_id)
        logger.info("chat settings updated", extra={"changes": sorted(changes)})
        return settings

    async def set_toggle(self, chat_id: int, toggle: Toggle, enabled: bool) -> ChatSettings:
        return await self.update(chat_id, **{toggle.value: enabled})

    # -- cache (failures degrade to the database) ---------------------------
    async def _cache_get(self, chat_id: int) -> ChatSettings | None:
        try:
            raw = await self._cache.get(_key(chat_id))
        except Exception:
            logger.warning("chat settings cache read failed", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return ChatSettings.model_validate_json(raw)
        except ValidationError:
            logger.warning("discarding malformed cached chat settings")
            return None

    async def _cache_set(self, chat_id: int, settings: ChatSettings) -> None:
        try:
            await self._cache.set(_key(chat_id), settings.model_dump_json(), self._cache_ttl)
        except Exception:
            logger.warning("chat settings cache write failed", exc_info=True)

    async def _cache_delete(self, chat_id: int) -> None:
        try:
            await self._cache.delete(_key(chat_id))
        except Exception:
            # Stale for at most cache_ttl seconds; the database already has the value.
            logger.warning("chat settings cache invalidation failed", exc_info=True)
