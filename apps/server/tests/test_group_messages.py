from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from akkiabot_core.db import Chat, ChatMember, ChatSettings, MessageLog, session_scope
from akkiabot_core.db.repositories import ChatRepository, MessageLogRepository
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.message_log import IncomingMessage, should_record
from akkiabot_server.testing import BOT_USER, FakeBotApi, UpdateFactory, metric_value

GROUP_ID = -1001234567890

_BASE = IncomingMessage(
    chat_id=GROUP_ID,
    chat_type="supergroup",
    chat_title="G",
    message_id=1,
    sender_id=42,
    sender_is_bot=False,
    is_service=False,
    date=datetime.now(UTC),
)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"chat_type": "group"}, True),
        ({"chat_type": "private"}, False),
        ({"chat_type": "channel"}, False),
        ({"sender_is_bot": True}, False),
        ({"sender_id": None}, False),
        ({"is_service": True}, False),
    ],
)
def test_should_record(changes: dict[str, object], expected: bool) -> None:
    assert should_record(replace(_BASE, **changes)) is expected  # pyright: ignore[reportArgumentType]


async def _logged(runtime: Runtime) -> list[tuple[int, int, int | None]]:
    async with session_scope(runtime.session_factory) as session:
        rows = await session.execute(
            select(MessageLog.chat_id, MessageLog.message_id, MessageLog.user_id).order_by(
                MessageLog.message_id
            )
        )
        return [tuple(r) for r in rows.all()]  # pyright: ignore[reportReturnType]


async def _chats(runtime: Runtime) -> list[Chat]:
    async with session_scope(runtime.session_factory) as session:
        return list((await session.scalars(select(Chat))).all())


async def test_group_message_is_recorded(runtime: Runtime, updates: UpdateFactory) -> None:
    group = updates.group(GROUP_ID, "My Group")
    first = updates.message("hello", chat=group, from_user=updates.user(42))
    second = updates.message("again", chat=group, from_user=updates.user(42))
    for update in (first, second):
        await runtime.application.process_update(update)

    assert await _logged(runtime) == [
        (GROUP_ID, first.message.message_id, 42),  # type: ignore[union-attr]
        (GROUP_ID, second.message.message_id, 42),  # type: ignore[union-attr]
    ]
    (chat,) = await _chats(runtime)
    assert (chat.id, chat.type, chat.title) == (GROUP_ID, "supergroup", "My Group")
    async with session_scope(runtime.session_factory) as session:
        member = await session.get(ChatMember, (GROUP_ID, 42))
        assert member is not None
        assert member.message_count == 2
        log = await MessageLogRepository(session).get(GROUP_ID, first.message.message_id)  # type: ignore[union-attr]
        assert log is not None
        assert log.text is None  # ids only


async def test_commands_are_recorded_too(runtime: Runtime, updates: UpdateFactory) -> None:
    await runtime.application.process_update(updates.command("/help"))
    assert len(await _logged(runtime)) == 1


async def test_bot_and_private_messages_are_not_recorded(
    runtime: Runtime, updates: UpdateFactory
) -> None:
    alice = updates.user(42)
    await runtime.application.process_update(
        updates.message("from a bot", from_user=updates.user(99, "Bot", is_bot=True))
    )
    await runtime.application.process_update(
        updates.message("private", chat=updates.private(alice), from_user=alice)
    )
    assert await _logged(runtime) == []
    # The group itself is still remembered; the private chat is not.
    assert [c.id for c in await _chats(runtime)] == [GROUP_ID]


async def test_service_message_deleted_and_not_logged(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    update = updates.service_message(new_chat_members=[updates.user(7, "Newbie")])
    await runtime.application.process_update(update)

    assert api.calls_to("deleteMessage") == [
        {"chat_id": GROUP_ID, "message_id": update.message.message_id}  # type: ignore[union-attr]
    ]
    assert await _logged(runtime) == []
    assert [c.id for c in await _chats(runtime)] == [GROUP_ID]


@pytest.mark.parametrize(
    "service",
    [
        {"left_chat_member": UpdateFactory.user(7)},
        {"pinned_message": {"message_id": 1, "date": 0, "chat": UpdateFactory.group(GROUP_ID)}},
        {"new_chat_title": "Renamed"},
    ],
)
async def test_other_service_messages_deleted(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, service: dict[str, Any]
) -> None:
    await runtime.application.process_update(updates.service_message(**service))
    assert len(api.calls_to("deleteMessage")) == 1


async def test_service_message_kept_when_disabled(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    async with session_scope(runtime.session_factory) as session:
        repo = ChatRepository(session)
        await repo.upsert(GROUP_ID, type="supergroup", title="G")
        await repo.update_settings(GROUP_ID, ChatSettings(delete_service_messages=False))
    await runtime.application.process_update(updates.service_message())
    assert api.calls_to("deleteMessage") == []


async def test_service_message_delete_failure_is_tolerated(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    api.fail("deleteMessage", "Bad Request: message can't be deleted")
    before = metric_value("akkiabot_handler_errors_total", handler="service_message")
    await runtime.application.process_update(updates.service_message())
    assert metric_value("akkiabot_handler_errors_total", handler="service_message") == before


async def test_private_service_message_ignored(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    alice = updates.user(42)
    await runtime.application.process_update(
        updates.service_message(chat=updates.private(alice), from_user=alice, new_chat_title="x")
    )
    assert api.calls_to("deleteMessage") == []


async def test_bot_added_to_group_records_chat(runtime: Runtime, updates: UpdateFactory) -> None:
    await runtime.application.process_update(
        updates.my_chat_member(chat=updates.group(GROUP_ID, "Old"), new_status="administrator")
    )
    await runtime.application.process_update(
        updates.my_chat_member(chat=updates.group(GROUP_ID, "New"), old_status="administrator")
    )
    (chat,) = await _chats(runtime)
    assert chat.title == "New"


async def test_bot_service_join_message_records_chat(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    await runtime.application.process_update(
        updates.service_message(chat=updates.group(-100999, "Fresh"), new_chat_members=[BOT_USER])
    )
    assert [c.id for c in await _chats(runtime)] == [-100999]


async def test_retention_cleanup(runtime: Runtime) -> None:
    now = datetime.now(UTC)
    async with session_scope(runtime.session_factory) as session:
        repo = MessageLogRepository(session)
        await repo.add(GROUP_ID, 1, user_id=1, created_at=now - timedelta(days=31))
        await repo.add(GROUP_ID, 2, user_id=1, created_at=now - timedelta(days=29))

    removed = await runtime.deps.messages.cleanup(timedelta(days=30), now=now)

    assert removed == 1
    assert [m for _, m, _ in await _logged(runtime)] == [2]


async def test_cleanup_job_is_scheduled_and_runs(runtime: Runtime) -> None:
    job_queue = runtime.application.job_queue
    assert job_queue is not None
    (job,) = job_queue.get_jobs_by_name("message_log_cleanup")
    old = datetime.now(UTC) - timedelta(
        days=runtime.settings.telegram.message_log_retention_days + 1
    )
    async with session_scope(runtime.session_factory) as session:
        await MessageLogRepository(session).add(GROUP_ID, 1, user_id=1, created_at=old)

    await job.run(runtime.application)

    async with session_scope(runtime.session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(MessageLog)) == 0
