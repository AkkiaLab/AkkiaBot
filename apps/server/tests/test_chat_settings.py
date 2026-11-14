from __future__ import annotations

from typing import Any, override

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.cache import MemoryCache
from akkiabot_core.db import ChatRepository, ChatSettings, session_scope
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.chat_settings import (
    KEY_PREFIX,
    ChatSettingsService,
    Toggle,
    UnknownChatError,
)

GROUP_ID = -1001234567890


async def _add_chat(factory: async_sessionmaker[AsyncSession], chat_id: int = GROUP_ID) -> None:
    async with session_scope(factory) as session:
        await ChatRepository(session).upsert(chat_id, type="supergroup", title="Group")


@pytest.fixture
def cache() -> MemoryCache:
    return MemoryCache()


@pytest.fixture
def service(runtime: Runtime, cache: MemoryCache) -> ChatSettingsService:
    return ChatSettingsService(runtime.session_factory, cache)


async def test_unknown_chat_has_defaults(service: ChatSettingsService) -> None:
    assert await service.get(GROUP_ID) == ChatSettings()
    assert await service.describe(GROUP_ID) is None
    with pytest.raises(UnknownChatError):
        await service.set_toggle(GROUP_ID, Toggle.WARN, True)


async def test_update_persists_and_invalidates_cache(
    runtime: Runtime, service: ChatSettingsService, cache: MemoryCache
) -> None:
    await _add_chat(runtime.session_factory)
    assert (await service.get(GROUP_ID)).warn is False  # now cached
    assert await cache.get(KEY_PREFIX + str(GROUP_ID)) is not None

    updated = await service.set_toggle(GROUP_ID, Toggle.WARN, True)
    assert updated.warn is True
    assert await cache.get(KEY_PREFIX + str(GROUP_ID)) is None  # invalidated
    assert (await service.get(GROUP_ID)).warn is True
    config = await service.describe(GROUP_ID)
    assert config is not None
    assert (config.title, config.settings.warn) == ("Group", True)
    # Other switches are untouched, and the JSON column holds the full validated model.
    async with session_scope(runtime.session_factory) as session:
        chat = await ChatRepository(session).get(GROUP_ID)
        assert chat is not None
        assert chat.settings == ChatSettings(warn=True).model_dump(mode="json")


async def test_get_is_served_from_cache(
    runtime: Runtime, service: ChatSettingsService, cache: MemoryCache
) -> None:
    await _add_chat(runtime.session_factory)
    await cache.set(KEY_PREFIX + str(GROUP_ID), ChatSettings(antispam=True).model_dump_json(), 60)
    assert (await service.get(GROUP_ID)).antispam is True


async def test_malformed_cache_entry_falls_back_to_database(
    runtime: Runtime, service: ChatSettingsService, cache: MemoryCache
) -> None:
    await _add_chat(runtime.session_factory)
    await cache.set(KEY_PREFIX + str(GROUP_ID), '{"max_warn_count": 0}', 60)
    assert await service.get(GROUP_ID) == ChatSettings()


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"max_warn_count": 0}, ValidationError),
        ({"warn": "maybe"}, ValidationError),
        ({"no_such_switch": True}, ValueError),
    ],
)
async def test_invalid_updates_are_rejected(
    runtime: Runtime, service: ChatSettingsService, changes: dict[str, Any], error: type[Exception]
) -> None:
    await _add_chat(runtime.session_factory)
    with pytest.raises(error):
        await service.update(GROUP_ID, **changes)
    assert await service.get(GROUP_ID) == ChatSettings()


class _BrokenCache(MemoryCache):
    @override
    async def get(self, key: str) -> str | None:
        raise ConnectionError("cache down")

    @override
    async def set(self, key: str, value: str, ttl: int) -> None:
        raise ConnectionError("cache down")

    @override
    async def delete(self, key: str) -> None:
        raise ConnectionError("cache down")


async def test_cache_outage_degrades_to_database(runtime: Runtime) -> None:
    await _add_chat(runtime.session_factory)
    service = ChatSettingsService(runtime.session_factory, _BrokenCache())
    await service.set_toggle(GROUP_ID, Toggle.ANTISPAM, True)
    assert (await service.get(GROUP_ID)).antispam is True
