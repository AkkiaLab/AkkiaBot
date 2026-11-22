import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut

from akkiabot_core.db import Chat, ChatMember, ChatSettings, MessageLog, session_scope
from akkiabot_core.db.repositories import ChatRepository, MessageLogRepository
from akkiabot_server.bot.handlers.group import expected_delete_failure, to_incoming
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


@pytest.mark.parametrize(
    ("status", "description", "reason"),
    [
        (400, "Bad Request: message can't be deleted", "no_permission"),
        (400, "Bad Request: message to delete not found", "already_deleted"),
        (400, "Bad Request: not enough rights to delete a message", "no_permission"),
        (403, "Forbidden: bot was kicked from the supergroup chat", "bot_not_in_chat"),
    ],
)
async def test_expected_delete_failure_is_info(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    caplog: pytest.LogCaptureFixture,
    status: int,
    description: str,
    reason: str,
) -> None:
    api.fail("deleteMessage", description, status=status)
    handler_errors = metric_value("akkiabot_handler_errors_total", handler="service_message")
    api_errors = metric_value("akkiabot_telegram_api_errors_total", method="deleteMessage")
    with caplog.at_level(logging.INFO):
        await runtime.application.process_update(updates.service_message())

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    (record,) = [r for r in caplog.records if r.message == "service message not deleted"]
    assert record.levelno == logging.INFO
    assert record.__dict__["reason"] == reason
    assert metric_value("akkiabot_handler_errors_total", handler="service_message") == (
        handler_errors
    )
    assert metric_value("akkiabot_telegram_api_errors_total", method="deleteMessage") == (
        api_errors
    )


@pytest.mark.parametrize(
    ("status", "description"),
    [
        (400, "Bad Request: some new error"),
        (500, "Internal Server Error"),
    ],
)
async def test_real_delete_failure_stays_warning(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    caplog: pytest.LogCaptureFixture,
    status: int,
    description: str,
) -> None:
    api.fail("deleteMessage", description, status=status)
    handler_errors = metric_value("akkiabot_handler_errors_total", handler="service_message")
    api_errors = metric_value("akkiabot_telegram_api_errors_total", method="deleteMessage")
    with caplog.at_level(logging.INFO):
        await runtime.application.process_update(updates.service_message())

    (record,) = [r for r in caplog.records if r.message == "failed to delete service message"]
    assert record.levelno == logging.WARNING
    # Tolerated (the handler does not crash) but still counted as an API error.
    assert metric_value("akkiabot_handler_errors_total", handler="service_message") == (
        handler_errors
    )
    assert metric_value("akkiabot_telegram_api_errors_total", method="deleteMessage") == (
        api_errors + 1
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (BadRequest("Message can't be deleted"), "no_permission"),
        (BadRequest("Message to delete not found"), "already_deleted"),
        (Forbidden("Forbidden: bot is not a member of the supergroup chat"), "bot_not_in_chat"),
        (BadRequest("Message is too long"), None),
        (TimedOut(), None),
        (NetworkError("connection reset"), None),
    ],
)
def test_expected_delete_failure(error: TelegramError, reason: str | None) -> None:
    assert expected_delete_failure(error) == reason


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


async def test_chat_is_recreated_after_rolled_back_first_message(
    runtime: Runtime, updates: UpdateFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a failed first transaction must not mark the chat as known."""
    calls = 0
    real_add = MessageLogRepository.add

    async def flaky_add(self: MessageLogRepository, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db hiccup")
        await real_add(self, *args, **kwargs)

    monkeypatch.setattr(MessageLogRepository, "add", flaky_add)
    group = updates.group(GROUP_ID, "Flaky")
    errors = metric_value("akkiabot_handler_errors_total", handler="record_message")

    await runtime.application.process_update(updates.message("first", chat=group))
    assert metric_value("akkiabot_handler_errors_total", handler="record_message") == errors + 1
    assert await _chats(runtime) == []  # rolled back

    await runtime.application.process_update(updates.message("second", chat=group))
    assert [c.id for c in await _chats(runtime)] == [GROUP_ID]
    assert len(await _logged(runtime)) == 1


async def test_known_chat_skips_lookup(
    runtime: Runtime, updates: UpdateFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    await runtime.application.process_update(updates.message("first"))
    lookups = 0
    real_get = ChatRepository.get

    async def counting_get(self: ChatRepository, chat_id: int) -> Any:
        nonlocal lookups
        lookups += 1
        return await real_get(self, chat_id)

    monkeypatch.setattr(ChatRepository, "get", counting_get)
    await runtime.application.process_update(updates.message("second"))
    assert lookups == 0


async def test_redelivered_message_counted_once(runtime: Runtime, updates: UpdateFactory) -> None:
    """Regression: webhook retries deliver the same message again."""
    first = updates.message("hello", from_user=updates.user(42))
    other = updates.message("next", from_user=updates.user(42))
    message = to_incoming(first.message, is_service=False)  # type: ignore[arg-type]

    assert await runtime.deps.messages.record(message) is True
    assert await runtime.deps.messages.record(message) is False  # duplicate
    await runtime.application.process_update(first)  # duplicate through the handler too
    await runtime.application.process_update(other)

    async with session_scope(runtime.session_factory) as session:
        member = await session.get(ChatMember, (GROUP_ID, 42))
        assert member is not None
        assert member.message_count == 2
    assert len(await _logged(runtime)) == 2
