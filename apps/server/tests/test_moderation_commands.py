"""``/ban`` and ``/warn`` through the real PTB application and a fake Bot API."""

from __future__ import annotations

import json
from typing import Any

import pytest

from akkiabot_core.db import ChatMemberRepository, MessageLogRepository, Restriction, session_scope
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.chat_settings import Toggle
from akkiabot_server.testing import (
    BOT_USER,
    FakeBotApi,
    UpdateFactory,
    member_responder,
    metric_value,
)

CHAT = -1001234567890
OWNER_ID, ADMIN_ID, WEAK_ADMIN_ID, MEMBER_ID, TARGET_ID, OTHER_ADMIN_ID = 1, 2, 3, 4, 42, 5

ROLES: dict[str, dict[str, Any]] = {
    "owner": UpdateFactory.chat_member(UpdateFactory.user(OWNER_ID, "Owner"), "creator"),
    "admin_can_ban": UpdateFactory.chat_member(
        UpdateFactory.user(ADMIN_ID, "Admin"), "administrator"
    ),
    "admin_cannot_ban": UpdateFactory.chat_member(
        UpdateFactory.user(WEAK_ADMIN_ID, "Weak"), "administrator", can_restrict_members=False
    ),
    "member": UpdateFactory.chat_member(UpdateFactory.user(MEMBER_ID, "Member")),
}
ACTOR_IDS = {
    "owner": OWNER_ID,
    "admin_can_ban": ADMIN_ID,
    "admin_cannot_ban": WEAK_ADMIN_ID,
    "member": MEMBER_ID,
}


@pytest.fixture
def members(api: FakeBotApi) -> dict[int, dict[str, Any]]:
    table = {ACTOR_IDS[role]: member for role, member in ROLES.items()}
    table[TARGET_ID] = UpdateFactory.chat_member(UpdateFactory.user(TARGET_ID, "Spam*Bot"))
    table[OTHER_ADMIN_ID] = UpdateFactory.chat_member(
        UpdateFactory.user(OTHER_ADMIN_ID, "Other"), "administrator", can_restrict_members=False
    )
    api.respond("getChatMember", member_responder(table))
    return table


def _deleted_ids(api: FakeBotApi) -> list[int]:
    ids: list[int] = []
    for params in api.calls_to("deleteMessages"):
        raw = params["message_ids"]
        ids += json.loads(raw) if isinstance(raw, str) else raw
    return sorted(ids)


class Group:
    """Builds group messages from the users above."""

    def __init__(self, updates: UpdateFactory) -> None:
        self.updates = updates
        self.chat = updates.group(CHAT, "Group")

    def said(self, user_id: int, text: str = "buy now", name: str = "Spam*Bot") -> dict[str, Any]:
        return self.updates.message_dict(
            chat=self.chat, from_user=self.updates.user(user_id, name), text=text
        )

    def command(
        self, text: str, *, by: int, reply_to: dict[str, Any] | None = None, **fields: Any
    ) -> Any:
        return self.updates.message(
            text,
            chat=self.chat,
            from_user=self.updates.user(by, "Actor"),
            reply_to=reply_to,
            **fields,
        )


@pytest.fixture
def group(updates: UpdateFactory) -> Group:
    return Group(updates)


async def _log_messages(runtime: Runtime, group: Group, count: int) -> list[dict[str, Any]]:
    """The target posts ``count`` messages that the bot records."""
    posted = []
    for i in range(count):
        message = group.said(TARGET_ID, f"spam {i}")
        posted.append(message)
        update = group.updates.message(message["text"], chat=group.chat, from_user=message["from"])
        await runtime.application.process_update(update)
        posted[-1] = update.message.to_dict()  # type: ignore[union-attr]
    return posted


# ---------------------------------------------------------------- /ban


@pytest.mark.parametrize(
    ("actor", "target_role", "expected"),
    [
        ("owner", "member", "banned"),
        ("owner", "admin", "无法封禁管理员或群主。"),
        ("admin_can_ban", "member", "banned"),
        ("admin_can_ban", "admin", "无法封禁管理员或群主。"),
        ("admin_cannot_ban", "member", "forbidden"),
        ("admin_cannot_ban", "admin", "forbidden"),
        ("member", "member", "forbidden"),
        ("member", "admin", "forbidden"),
    ],
)
async def test_ban_permission_matrix(
    runtime: Runtime,
    api: FakeBotApi,
    group: Group,
    members: dict[int, dict[str, Any]],
    actor: str,
    target_role: str,
    expected: str,
) -> None:
    target_id = TARGET_ID if target_role == "member" else OTHER_ADMIN_ID
    await runtime.application.process_update(
        group.command("/ban", by=ACTOR_IDS[actor], reply_to=group.said(target_id))
    )
    banned = api.calls_to("banChatMember")
    if expected == "banned":
        assert [int(c["user_id"]) for c in banned] == [target_id]
        assert "已被封禁" in api.sent_texts()[-1]
    else:
        assert banned == []
        assert api.calls_to("deleteMessages") == []
        if expected == "forbidden":
            assert api.sent_texts() == [
                "你没有权限封禁成员：仅群主或具有封禁成员权限的管理员可用。"
            ]
        else:
            assert api.sent_texts() == [expected]


async def test_ban_deletes_history_and_records_restriction(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    posted = await _log_messages(runtime, group, 3)
    api.clear()
    bans = metric_value("akkiabot_command_total", command="ban", result="ok")

    await runtime.application.process_update(
        group.command("/ban", by=OWNER_ID, reply_to=posted[-1])
    )

    assert [int(c["user_id"]) for c in api.calls_to("banChatMember")] == [TARGET_ID]
    assert _deleted_ids(api) == sorted(m["message_id"] for m in posted)
    reply = api.calls_to("sendMessage")[-1]
    assert reply["parse_mode"] == "MarkdownV2"
    # The name is escaped inside the mention link.
    assert reply["text"] == (f"[Spam\\*Bot](tg://user?id={TARGET_ID}) 已被封禁，删除了 3 条消息。")
    async with session_scope(runtime.session_factory) as session:
        member = await ChatMemberRepository(session).get(CHAT, TARGET_ID)
        assert member is not None
        assert (member.restriction, member.restricted_by) == (Restriction.ADMIN_BAN, OWNER_ID)
        assert await MessageLogRepository(session).list_message_ids(CHAT, TARGET_ID) == []
    assert metric_value("akkiabot_command_total", command="ban", result="ok") == bans + 1


async def test_ban_failure_is_reported(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    api.fail("banChatMember", "Bad Request: not enough rights to restrict/unrestrict chat member")
    await runtime.application.process_update(
        group.command("/ban", by=OWNER_ID, reply_to=group.said(TARGET_ID))
    )
    assert api.sent_texts() == ["封禁失败，请确认机器人具有封禁成员权限。"]
    assert api.calls_to("deleteMessages") == []


async def test_ban_requires_a_reply(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    await runtime.application.process_update(group.command("/ban", by=OWNER_ID))
    assert api.sent_texts() == [
        "此命令需要以回复的方式使用，将封禁被回复的成员。\n用法：/ban（回复要封禁的成员的消息）"
    ]
    assert api.calls_to("getChatMember") == []


async def test_ban_rejects_the_bot_itself_and_channels(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    bot_message = group.updates.message_dict(chat=group.chat, from_user=BOT_USER, text="hi")
    await runtime.application.process_update(
        group.command("/ban", by=OWNER_ID, reply_to=bot_message)
    )
    channel_post = group.said(TARGET_ID) | {
        "sender_chat": {"id": -100777, "type": "channel", "title": "Ch"}
    }
    await runtime.application.process_update(
        group.command("/ban", by=OWNER_ID, reply_to=channel_post)
    )
    assert api.sent_texts() == [
        "不能对机器人自己执行此操作。",
        "无法对频道或匿名管理员的消息执行此操作。",
    ]
    assert api.calls_to("banChatMember") == []


async def test_anonymous_admin_is_asked_to_reveal(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    update = group.command(
        "/ban", by=1087968824, reply_to=group.said(TARGET_ID), sender_chat=group.chat
    )
    await runtime.application.process_update(update)
    assert api.sent_texts() == ["匿名管理员无法使用此命令，请先关闭匿名身份再试。"]


async def test_ban_in_private_chat(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    user = updates.user(OWNER_ID)
    await runtime.application.process_update(
        updates.command("/ban", chat=updates.private(user), from_user=user)
    )
    assert api.sent_texts() == ["请在群组中使用此命令。"]


# ---------------------------------------------------------------- /warn


async def _enable_warn(runtime: Runtime, group: Group) -> None:
    # Any group message makes the chat known; then flip the switch.
    await runtime.application.process_update(
        group.updates.message("hi", chat=group.chat, from_user=group.updates.user(OWNER_ID))
    )
    await runtime.deps.chat_settings.set_toggle(CHAT, Toggle.WARN, True)


async def test_warn_requires_the_switch(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    await runtime.application.process_update(
        group.command("/warn", by=OWNER_ID, reply_to=group.said(TARGET_ID))
    )
    assert api.sent_texts() == ["请先通过 /config 开启警告功能。"]
    assert api.calls_to("getChatMember") == []


async def test_warn_until_banned(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    await _enable_warn(runtime, group)
    posted = await _log_messages(runtime, group, 2)
    api.clear()

    for _ in range(3):
        await runtime.application.process_update(
            group.command("/warn", by=ADMIN_ID, reply_to=posted[-1])
        )

    mention = f"[Spam\\*Bot](tg://user?id={TARGET_ID})"
    assert api.sent_texts() == [
        f"{mention} 你已被警告 1/3 次",
        f"{mention} 你已被警告 2/3 次",
        f"{mention} 你已被警告 3/3 次，警告次数已达上限，已被封禁。\n删除了 2 条消息。",
    ]
    assert [int(c["user_id"]) for c in api.calls_to("banChatMember")] == [TARGET_ID]
    assert _deleted_ids(api) == sorted(m["message_id"] for m in posted)
    async with session_scope(runtime.session_factory) as session:
        member = await ChatMemberRepository(session).get(CHAT, TARGET_ID)
        assert member is not None
        assert (member.warn_count, member.restriction) == (0, Restriction.WARN_BAN)


@pytest.mark.parametrize(
    ("actor", "target_role", "expected"),
    [
        ("owner", "member", "你已被警告 1/3 次"),
        ("owner", "admin", "无法警告管理员或群主。"),
        ("admin_can_ban", "member", "你已被警告 1/3 次"),
        ("admin_can_ban", "admin", "无法警告管理员或群主。"),
        ("admin_cannot_ban", "member", "你没有权限警告成员"),
        ("admin_cannot_ban", "admin", "你没有权限警告成员"),
        ("member", "member", "你没有权限警告成员"),
        ("member", "admin", "你没有权限警告成员"),
    ],
)
async def test_warn_permission_matrix(
    runtime: Runtime,
    api: FakeBotApi,
    group: Group,
    members: dict[int, dict[str, Any]],
    actor: str,
    target_role: str,
    expected: str,
) -> None:
    await _enable_warn(runtime, group)
    target_id = TARGET_ID if target_role == "member" else OTHER_ADMIN_ID
    await runtime.application.process_update(
        group.command("/warn", by=ACTOR_IDS[actor], reply_to=group.said(target_id))
    )
    assert expected in api.sent_texts()[-1]
    async with session_scope(runtime.session_factory) as session:
        member = await ChatMemberRepository(session).get(CHAT, target_id)
    warned = member.warn_count if member else 0
    assert warned == (1 if expected.startswith("你已被警告") else 0)


async def test_warn_ban_failure_keeps_count(
    runtime: Runtime, api: FakeBotApi, group: Group, members: dict[int, dict[str, Any]]
) -> None:
    await _enable_warn(runtime, group)
    await runtime.deps.chat_settings.update(CHAT, max_warn_count=1)
    api.fail("banChatMember", "Bad Request: not enough rights")
    await runtime.application.process_update(
        group.command("/warn", by=OWNER_ID, reply_to=group.said(TARGET_ID))
    )
    assert api.sent_texts()[-1] == (
        "对方的警告次数已达上限，但封禁失败，请确认机器人具有封禁成员权限。"
    )
    async with session_scope(runtime.session_factory) as session:
        member = await ChatMemberRepository(session).get(CHAT, TARGET_ID)
        assert member is not None
        assert (member.warn_count, member.restriction) == (1, Restriction.NONE)
