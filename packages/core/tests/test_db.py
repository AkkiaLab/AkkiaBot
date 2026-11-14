"""Migration + repository integration tests (SQLite always, PostgreSQL when configured)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from akkiabot_core.db import (
    Base,
    CaptchaChallengeRepository,
    CaptchaStatus,
    Chat,
    ChatMemberRepository,
    ChatRepository,
    ChatSettings,
    MessageLogRepository,
    Restriction,
    create_session_factory,
    ping,
    session_scope,
)
from akkiabot_core.db.migrate import upgrade, upgrade_engine

# Supergroup ids are 13 digits starting with -100 and overflow INT32.
SUPERGROUP = -1001234567890
BIG_USER = 7_123_456_789  # user ids already exceed 2**31
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
async def engine(empty_engine: AsyncEngine) -> AsyncEngine:
    await upgrade_engine(empty_engine)
    return empty_engine


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = create_session_factory(engine)
    async with factory() as s:
        yield s


# --------------------------------------------------------------- migrations


async def test_migration_creates_tables(engine: AsyncEngine) -> None:
    def tables(conn: Connection) -> set[str]:
        return set(inspect(conn).get_table_names())

    async with engine.connect() as conn:
        names = await conn.run_sync(tables)
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    assert {"chat", "chat_member", "message_log", "captcha_challenge"} <= names
    assert version == "0001"


async def test_migration_matches_models(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[object]:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []


async def test_sync_upgrade_entry_point(db_url: str, empty_engine: AsyncEngine) -> None:
    # `akkiabot migrate` path: its own engine + asyncio.run, so run in a thread.
    await asyncio.to_thread(upgrade, db_url)
    await asyncio.to_thread(upgrade, db_url)  # idempotent
    async with empty_engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    assert version == "0001"


async def test_ping(engine: AsyncEngine) -> None:
    assert await ping(engine)


# ------------------------------------------------------------- repositories


async def test_chat_upsert_with_supergroup_id(session: AsyncSession) -> None:
    repo = ChatRepository(session)
    chat = await repo.upsert(SUPERGROUP, type="supergroup", title="测试群")
    await session.commit()
    assert chat.id == SUPERGROUP
    assert chat.settings == {}
    created = chat.created_at
    assert created.tzinfo is not None

    chat = await repo.upsert(SUPERGROUP, type="supergroup", title="改名后")
    await session.commit()
    session.expunge_all()

    loaded = await repo.get(SUPERGROUP)
    assert loaded is not None
    assert loaded.title == "改名后"
    assert loaded.created_at == created
    assert loaded.updated_at >= created
    raw = await session.scalar(select(Chat.id).where(Chat.id == SUPERGROUP))
    assert raw == SUPERGROUP


async def test_chat_settings_roundtrip(session: AsyncSession) -> None:
    repo = ChatRepository(session)
    assert await repo.get_settings(SUPERGROUP) == ChatSettings()

    await repo.upsert(SUPERGROUP, type="supergroup", title=None)
    new = ChatSettings(antispam=True, captcha=True, max_warn_count=5)
    await repo.update_settings(SUPERGROUP, new)
    await session.commit()
    session.expunge_all()

    assert await repo.get_settings(SUPERGROUP) == new
    # upsert must not reset settings
    await repo.upsert(SUPERGROUP, type="supergroup", title="t")
    await session.commit()
    assert (await repo.get_settings(SUPERGROUP)).antispam is True


async def test_member_counters_and_restriction(session: AsyncSession) -> None:
    repo = ChatMemberRepository(session)
    await repo.record_message(SUPERGROUP, BIG_USER, at=NOW)
    await repo.record_message(SUPERGROUP, BIG_USER, at=NOW + timedelta(minutes=1))
    await session.commit()
    session.expunge_all()

    member = await repo.get(SUPERGROUP, BIG_USER)
    assert member is not None
    assert member.message_count == 2
    assert member.last_message_at == NOW + timedelta(minutes=1)
    assert member.restriction is Restriction.NONE

    assert await repo.add_warning(SUPERGROUP, BIG_USER) == 1
    assert await repo.add_warning(SUPERGROUP, BIG_USER) == 2
    await repo.set_restriction(
        SUPERGROUP, BIG_USER, Restriction.CAPTCHA_MUTE, restricted_by=1, until=NOW
    )
    await session.commit()
    session.expunge_all()

    member = await repo.get(SUPERGROUP, BIG_USER)
    assert member is not None
    assert member.warn_count == 2
    assert member.restriction is Restriction.CAPTCHA_MUTE
    assert member.restricted_until == NOW

    await repo.reset_warnings(SUPERGROUP, BIG_USER)
    await session.commit()
    session.expunge_all()
    member = await repo.get(SUPERGROUP, BIG_USER)
    assert member is not None
    assert member.warn_count == 0


async def test_member_get_or_create_keeps_existing(session: AsyncSession) -> None:
    repo = ChatMemberRepository(session)
    first = await repo.get_or_create(SUPERGROUP, 1, joined_at=NOW)
    await repo.record_message(SUPERGROUP, 1)
    await session.commit()
    again = await repo.get_or_create(SUPERGROUP, 1, joined_at=NOW + timedelta(days=1))
    assert again.joined_at == first.joined_at == NOW


async def test_message_log(session: AsyncSession) -> None:
    repo = MessageLogRepository(session)
    for message_id in (3, 1, 2):
        assert await repo.add(SUPERGROUP, message_id, user_id=BIG_USER, created_at=NOW) is True
    assert await repo.add(SUPERGROUP, 1, user_id=BIG_USER) is False  # duplicate ignored
    await repo.add(SUPERGROUP, 4, user_id=99, created_at=NOW - timedelta(days=30))
    await repo.add(-1009999999999, 1, user_id=BIG_USER, created_at=NOW)
    await session.commit()

    assert list(await repo.list_message_ids(SUPERGROUP, BIG_USER)) == [1, 2, 3]

    await repo.mark_spam(SUPERGROUP, 2, verdict={"is_spam": True, "category": "ad"}, text="buy")
    await session.commit()
    session.expunge_all()
    flagged = await repo.get(SUPERGROUP, 2)
    assert flagged is not None
    assert flagged.spam_verdict == {"is_spam": True, "category": "ad"}
    assert flagged.text == "buy"
    plain = await repo.get(SUPERGROUP, 1)
    assert plain is not None
    assert plain.text is None

    assert await repo.delete_older_than(NOW - timedelta(days=7)) == 1
    assert await repo.delete_messages(SUPERGROUP, [1, 3]) == 2
    assert await repo.delete_messages(SUPERGROUP, []) == 0
    await session.commit()
    assert list(await repo.list_message_ids(SUPERGROUP, BIG_USER)) == [2]


async def test_captcha_challenge_lifecycle(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    challenge = await repo.create(
        SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5), group_message_id=555
    )
    await session.commit()
    assert isinstance(challenge.id, uuid.UUID)
    assert challenge.expires_at - challenge.created_at == timedelta(minutes=5)

    pending = await repo.get_pending(SUPERGROUP, BIG_USER)
    assert pending is not None
    assert pending.id == challenge.id

    assert await repo.resolve(challenge.id, CaptchaStatus.PASSED) is True
    assert await repo.resolve(challenge.id, CaptchaStatus.PASSED) is False  # only once
    await session.commit()
    session.expunge_all()
    loaded = await repo.get(challenge.id)
    assert loaded is not None
    assert loaded.status is CaptchaStatus.PASSED
    assert await repo.get_pending(SUPERGROUP, BIG_USER) is None


@pytest.mark.parametrize("status", [CaptchaStatus.PASSED, CaptchaStatus.FAILED])
async def test_captcha_cannot_pass_after_expiry(
    session: AsyncSession, status: CaptchaStatus
) -> None:
    repo = CaptchaChallengeRepository(session)
    challenge = await repo.create(SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5))
    await session.commit()

    # exactly at expires_at counts as expired
    assert await repo.resolve(challenge.id, status, now=challenge.expires_at) is False
    # overdue but not yet swept by expire_due: real clock
    overdue = await repo.create(SUPERGROUP, BIG_USER + 1, ttl=timedelta(seconds=-1))
    await session.commit()
    assert await repo.resolve(overdue.id, status) is False

    # still pending, so it can be expired
    assert await repo.resolve(challenge.id, CaptchaStatus.EXPIRED) is True
    assert await repo.resolve(overdue.id, CaptchaStatus.EXPIRED) is True
    await session.commit()
    session.expunge_all()
    for cid in (challenge.id, overdue.id):
        loaded = await repo.get(cid)
        assert loaded is not None
        assert loaded.status is CaptchaStatus.EXPIRED


async def test_captcha_can_pass_just_before_expiry(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    challenge = await repo.create(SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5))
    await session.commit()
    just_before = challenge.expires_at - timedelta(microseconds=1)
    assert await repo.resolve(challenge.id, CaptchaStatus.PASSED, now=just_before) is True


async def test_captcha_resolve_to_pending_rejected(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    challenge = await repo.create(SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5))
    with pytest.raises(ValueError, match="PENDING"):
        await repo.resolve(challenge.id, CaptchaStatus.PENDING)


async def test_captcha_expire_due(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    old = await repo.create(SUPERGROUP, 1, ttl=timedelta(minutes=5))
    fresh = await repo.create(SUPERGROUP, 2, ttl=timedelta(minutes=5))
    await session.commit()

    expired = await repo.expire_due(now=max(old.expires_at, fresh.expires_at))
    ids = {c.id for c in expired}
    assert ids == {old.id, fresh.id}
    assert all(c.status is CaptchaStatus.EXPIRED for c in expired)
    await session.commit()
    assert await repo.expire_due(now=fresh.expires_at + timedelta(hours=1)) == []


async def test_captcha_expire_due_skips_future(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    challenge = await repo.create(SUPERGROUP, 1, ttl=timedelta(minutes=5))
    await session.commit()
    assert await repo.expire_due(now=challenge.created_at + timedelta(minutes=1)) == []


async def test_session_scope_commits_and_rolls_back(engine: AsyncEngine) -> None:
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with session_scope(factory) as s:
        await ChatRepository(s).upsert(SUPERGROUP, type="supergroup", title="a")

    async def failing_unit_of_work() -> None:
        async with session_scope(factory) as s:
            await ChatRepository(s).upsert(-1000000000001, type="supergroup", title="b")
            raise RuntimeError

    with pytest.raises(RuntimeError):
        await failing_unit_of_work()

    async with factory() as s:
        assert await ChatRepository(s).get(SUPERGROUP) is not None
        assert await ChatRepository(s).get(-1000000000001) is None


async def test_naive_datetime_rejected(session: AsyncSession) -> None:
    from sqlalchemy.exc import StatementError

    with pytest.raises(StatementError, match="naive"):
        await MessageLogRepository(session).add(
            SUPERGROUP,
            1,
            user_id=1,
            created_at=datetime(2026, 1, 1),
        )


async def test_captcha_list_pending_and_group_message(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    first = await repo.create(SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5))
    second = await repo.create(SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5))
    other = await repo.create(SUPERGROUP, BIG_USER + 1, ttl=timedelta(minutes=5))
    await repo.set_group_message(second.id, 4242)
    assert await repo.resolve(first.id, CaptchaStatus.EXPIRED) is True
    await session.commit()
    session.expunge_all()

    pending = await repo.list_pending(SUPERGROUP, BIG_USER)
    assert [c.id for c in pending] == [second.id]
    assert pending[0].group_message_id == 4242
    assert [c.id for c in await repo.list_pending(SUPERGROUP, BIG_USER + 1)] == [other.id]


async def test_captcha_reopen_only_unexpired_passed(session: AsyncSession) -> None:
    repo = CaptchaChallengeRepository(session)
    challenge = await repo.create(SUPERGROUP, BIG_USER, ttl=timedelta(minutes=5))
    await session.commit()

    assert await repo.reopen(challenge.id) is False  # still pending: nothing to undo
    assert await repo.resolve(challenge.id, CaptchaStatus.PASSED) is True
    assert await repo.reopen(challenge.id, now=challenge.expires_at) is False  # too late
    assert await repo.reopen(challenge.id) is True
    assert await repo.resolve(challenge.id, CaptchaStatus.PASSED) is True  # can be retried
    await session.commit()
