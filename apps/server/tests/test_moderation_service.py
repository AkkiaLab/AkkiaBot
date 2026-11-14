"""ModerationService with an in-memory ModerationApi (no Telegram)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.cache import MemoryCache
from akkiabot_core.db import (
    ChatMember,
    ChatMemberRepository,
    ChatRepository,
    MessageLogRepository,
    Restriction,
    session_scope,
)
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.chat_settings import ChatSettingsService, Toggle
from akkiabot_server.services.moderation import (
    Authorization,
    MemberInfo,
    MemberRole,
    ModerationApiError,
    ModerationService,
    RateLimitedError,
    WarnDisabledError,
    authorize,
)
from akkiabot_server.testing import metric_value

CHAT = -1001234567890
ADMIN, SPAMMER = 1, 42

OWNER = MemberInfo(MemberRole.OWNER)
ADMIN_CAN_BAN = MemberInfo(MemberRole.ADMIN, can_restrict_members=True)
ADMIN_CANNOT_BAN = MemberInfo(MemberRole.ADMIN, can_restrict_members=False)
MEMBER = MemberInfo(MemberRole.MEMBER)
ACTORS = {
    "owner": OWNER,
    "admin_can_ban": ADMIN_CAN_BAN,
    "admin_cannot_ban": ADMIN_CANNOT_BAN,
    "member": MEMBER,
}
TARGETS = {"admin": ADMIN_CANNOT_BAN, "owner": OWNER, "member": MEMBER}


@pytest.mark.parametrize(
    ("actor", "target", "expected"),
    [
        ("owner", "admin", Authorization.TARGET_PROTECTED),
        ("owner", "owner", Authorization.TARGET_PROTECTED),
        ("owner", "member", Authorization.ALLOWED),
        ("admin_can_ban", "admin", Authorization.TARGET_PROTECTED),
        ("admin_can_ban", "owner", Authorization.TARGET_PROTECTED),
        ("admin_can_ban", "member", Authorization.ALLOWED),
        ("admin_cannot_ban", "admin", Authorization.ACTOR_FORBIDDEN),
        ("admin_cannot_ban", "owner", Authorization.ACTOR_FORBIDDEN),
        ("admin_cannot_ban", "member", Authorization.ACTOR_FORBIDDEN),
        ("member", "admin", Authorization.ACTOR_FORBIDDEN),
        ("member", "owner", Authorization.ACTOR_FORBIDDEN),
        ("member", "member", Authorization.ACTOR_FORBIDDEN),
    ],
)
def test_permission_matrix(actor: str, target: str, expected: Authorization) -> None:
    assert authorize(ACTORS[actor], TARGETS[target]) is expected


@pytest.mark.parametrize("role", [MemberRole.RESTRICTED, MemberRole.LEFT, MemberRole.BANNED])
def test_non_members_are_neither_moderators_nor_protected(role: MemberRole) -> None:
    info = MemberInfo(role, can_restrict_members=True)
    assert not info.can_moderate
    assert authorize(OWNER, info) is Authorization.ALLOWED


@dataclass
class FakeApi:
    banned: list[tuple[int, int]] = field(default_factory=list)
    deleted: list[list[int]] = field(default_factory=list)
    ban_errors: list[Exception] = field(default_factory=list)
    delete_errors: dict[int, list[Exception]] = field(default_factory=dict)
    """Errors raised (once each) for batches starting with the given id."""
    in_flight: int = 0
    max_in_flight: int = 0

    async def get_member(self, chat_id: int, user_id: int) -> MemberInfo:
        return MEMBER

    async def ban(self, chat_id: int, user_id: int) -> None:
        if self.ban_errors:
            raise self.ban_errors.pop(0)
        self.banned.append((chat_id, user_id))

    async def delete_messages(self, chat_id: int, message_ids: Sequence[int]) -> None:
        assert len(message_ids) <= 100
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            pending = self.delete_errors.get(message_ids[0])
            if pending:
                raise pending.pop(0)
            self.deleted.append(list(message_ids))
        finally:
            self.in_flight -= 1


@pytest.fixture
def chat_settings(runtime: Runtime) -> ChatSettingsService:
    return ChatSettingsService(runtime.session_factory, MemoryCache())


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def service(
    runtime: Runtime, chat_settings: ChatSettingsService, sleeps: list[float]
) -> ModerationService:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return ModerationService(
        runtime.session_factory, chat_settings, delete_concurrency=2, sleep=fake_sleep
    )


async def _setup_chat(
    factory: async_sessionmaker[AsyncSession], *, messages: int = 0, warn: bool = True
) -> None:
    async with session_scope(factory) as session:
        await ChatRepository(session).upsert(CHAT, type="supergroup", title="G")
        log = MessageLogRepository(session)
        for message_id in range(1, messages + 1):
            await log.add(CHAT, message_id, user_id=SPAMMER)
        await log.add(CHAT, 10_000, user_id=ADMIN)  # someone else's message: never deleted
    if warn:
        await ChatSettingsService(factory, MemoryCache()).set_toggle(CHAT, Toggle.WARN, True)


async def _member(factory: async_sessionmaker[AsyncSession]) -> ChatMember | None:
    async with session_scope(factory) as session:
        return await ChatMemberRepository(session).get(CHAT, SPAMMER)


async def _logged(factory: async_sessionmaker[AsyncSession], user_id: int) -> list[int]:
    async with session_scope(factory) as session:
        return list(await MessageLogRepository(session).list_message_ids(CHAT, user_id))


async def test_ban_purges_history_in_limited_batches(
    runtime: Runtime, service: ModerationService
) -> None:
    await _setup_chat(runtime.session_factory, messages=250)
    api = FakeApi()
    bans = metric_value("akkiabot_moderation_actions_total", action="ban")
    deletes = metric_value("akkiabot_moderation_actions_total", action="delete")

    result = await service.ban(api, CHAT, SPAMMER, by=ADMIN, extra_message_ids=[999])

    assert api.banned == [(CHAT, SPAMMER)]
    assert sorted(len(b) for b in api.deleted) == [51, 100, 100]
    assert sorted(i for b in api.deleted for i in b) == [*range(1, 251), 999]
    assert api.max_in_flight == 2
    assert (result.purge.deleted, result.purge.failed) == (251, 0)
    member = await _member(runtime.session_factory)
    assert member is not None
    assert (member.restriction, member.restricted_by) == (Restriction.ADMIN_BAN, ADMIN)
    assert await _logged(runtime.session_factory, SPAMMER) == []
    assert await _logged(runtime.session_factory, ADMIN) == [10_000]
    assert metric_value("akkiabot_moderation_actions_total", action="ban") == bans + 1
    assert metric_value("akkiabot_moderation_actions_total", action="delete") == deletes + 251


async def test_purge_backs_off_on_rate_limit_and_keeps_failed_rows(
    runtime: Runtime, service: ModerationService, sleeps: list[float]
) -> None:
    await _setup_chat(runtime.session_factory, messages=150)
    api = FakeApi(
        delete_errors={
            1: [RateLimitedError(3), RateLimitedError(5)],  # retried, then succeeds
            101: [ModerationApiError("message can't be deleted")],  # gives up on this batch
        }
    )
    errors = metric_value("akkiabot_telegram_api_errors_total", method="deleteMessages")

    result = await service.ban(api, CHAT, SPAMMER, by=ADMIN)

    assert sleeps == [3, 5]
    assert (result.purge.deleted, result.purge.failed) == (100, 50)
    assert await _logged(runtime.session_factory, SPAMMER) == list(range(101, 151))
    assert metric_value("akkiabot_telegram_api_errors_total", method="deleteMessages") == errors + 3


async def test_rate_limit_retries_are_bounded(
    runtime: Runtime, service: ModerationService, sleeps: list[float]
) -> None:
    await _setup_chat(runtime.session_factory)
    api = FakeApi(ban_errors=[RateLimitedError(1)] * 4)
    with pytest.raises(RateLimitedError):
        await service.ban(api, CHAT, SPAMMER, by=ADMIN)
    assert sleeps == [1, 1, 1]
    assert await _member(runtime.session_factory) is None  # nothing recorded


async def test_overlong_rate_limit_is_not_waited(
    runtime: Runtime, service: ModerationService, sleeps: list[float]
) -> None:
    await _setup_chat(runtime.session_factory)
    with pytest.raises(RateLimitedError):
        await service.ban(FakeApi(ban_errors=[RateLimitedError(3600)]), CHAT, SPAMMER, by=ADMIN)
    assert sleeps == []


async def test_failed_ban_records_nothing(runtime: Runtime, service: ModerationService) -> None:
    await _setup_chat(runtime.session_factory, messages=3)
    api = FakeApi(ban_errors=[ModerationApiError("not enough rights")])
    with pytest.raises(ModerationApiError):
        await service.ban(api, CHAT, SPAMMER, by=ADMIN)
    assert api.deleted == []
    assert await _member(runtime.session_factory) is None
    assert await _logged(runtime.session_factory, SPAMMER) == [1, 2, 3]


async def test_warn_until_limit_bans_and_resets(
    runtime: Runtime, service: ModerationService
) -> None:
    await _setup_chat(runtime.session_factory, messages=2)
    api = FakeApi()
    warns = metric_value("akkiabot_moderation_actions_total", action="warn")

    first = await service.warn(api, CHAT, SPAMMER, by=ADMIN)
    second = await service.warn(api, CHAT, SPAMMER, by=ADMIN)
    assert [(r.count, r.limit, r.banned) for r in (first, second)] == [(1, 3, False), (2, 3, False)]
    assert api.banned == []
    member = await _member(runtime.session_factory)
    assert member is not None
    assert (member.warn_count, member.restriction) == (2, Restriction.NONE)

    third = await service.warn(api, CHAT, SPAMMER, by=ADMIN)
    assert (third.count, third.limit, third.banned) == (3, 3, True)
    assert third.purge is not None
    assert third.purge.deleted == 2
    assert api.banned == [(CHAT, SPAMMER)]
    member = await _member(runtime.session_factory)
    assert member is not None
    assert (member.warn_count, member.restriction, member.restricted_by) == (
        0,
        Restriction.WARN_BAN,
        ADMIN,
    )
    assert await _logged(runtime.session_factory, SPAMMER) == []
    assert metric_value("akkiabot_moderation_actions_total", action="warn") == warns + 3


async def test_warn_respects_configured_limit(
    runtime: Runtime, service: ModerationService, chat_settings: ChatSettingsService
) -> None:
    await _setup_chat(runtime.session_factory)
    await chat_settings.update(CHAT, max_warn_count=1)
    result = await service.warn(FakeApi(), CHAT, SPAMMER, by=ADMIN)
    assert (result.count, result.limit, result.banned) == (1, 1, True)


async def test_failed_ban_at_limit_keeps_the_count(
    runtime: Runtime, service: ModerationService
) -> None:
    await _setup_chat(runtime.session_factory)
    api = FakeApi()
    for _ in range(2):
        await service.warn(api, CHAT, SPAMMER, by=ADMIN)
    api.ban_errors.append(ModerationApiError("not enough rights"))
    with pytest.raises(ModerationApiError):
        await service.warn(api, CHAT, SPAMMER, by=ADMIN)
    member = await _member(runtime.session_factory)
    assert member is not None
    assert (member.warn_count, member.restriction) == (3, Restriction.NONE)
    # The next warning retries the ban and then resets.
    retried = await service.warn(api, CHAT, SPAMMER, by=ADMIN)
    assert retried.banned
    member = await _member(runtime.session_factory)
    assert member is not None
    assert (member.warn_count, member.restriction) == (0, Restriction.WARN_BAN)


async def test_warn_requires_the_switch(runtime: Runtime, service: ModerationService) -> None:
    await _setup_chat(runtime.session_factory, warn=False)
    with pytest.raises(WarnDisabledError):
        await service.warn(FakeApi(), CHAT, SPAMMER, by=ADMIN)
    assert await _member(runtime.session_factory) is None


def test_invalid_configuration(runtime: Runtime, chat_settings: ChatSettingsService) -> None:
    with pytest.raises(ValueError, match="delete_concurrency"):
        ModerationService(runtime.session_factory, chat_settings, delete_concurrency=0)
