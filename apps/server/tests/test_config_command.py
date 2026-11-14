"""``/config``: group entry, private menu via one-time ``/start`` link, toggle callbacks."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from akkiabot_core.db import ChatSettings
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.chat_settings import Toggle
from akkiabot_server.testing import (
    BOT_USERNAME,
    FakeBotApi,
    UpdateFactory,
    member_responder,
    metric_value,
)

CHAT = -1001234567890
OWNER_ID, ADMIN_ID, WEAK_ADMIN_ID, MEMBER_ID = 1, 2, 3, 4


@pytest.fixture
def members(api: FakeBotApi) -> dict[int, dict[str, Any]]:
    table = {
        OWNER_ID: UpdateFactory.chat_member(UpdateFactory.user(OWNER_ID), "creator"),
        ADMIN_ID: UpdateFactory.chat_member(UpdateFactory.user(ADMIN_ID), "administrator"),
        WEAK_ADMIN_ID: UpdateFactory.chat_member(
            UpdateFactory.user(WEAK_ADMIN_ID), "administrator", can_restrict_members=False
        ),
        MEMBER_ID: UpdateFactory.chat_member(UpdateFactory.user(MEMBER_ID)),
    }
    api.respond("getChatMember", member_responder(table))
    return table


@pytest.fixture
def group(updates: UpdateFactory) -> dict[str, Any]:
    return updates.group(CHAT, "My Group")


def _markup(params: dict[str, Any]) -> list[list[dict[str, Any]]]:
    raw = params["reply_markup"]
    data = json.loads(raw) if isinstance(raw, str) else raw
    return data["inline_keyboard"]


async def _config_in_group(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, group: dict[str, Any], user_id: int
) -> str | None:
    """Send ``/config`` in the group; returns the start token of the deep link, if any."""
    api.clear()
    await runtime.application.process_update(
        updates.command("/config", chat=group, from_user=updates.user(user_id))
    )
    (reply,) = api.calls_to("sendMessage")
    if "reply_markup" not in reply:
        return None
    ((button,),) = _markup(reply)
    url = urlparse(button["url"])
    assert (url.netloc, url.path) == ("t.me", f"/{BOT_USERNAME}")
    return parse_qs(url.query)["start"][0]


async def _start(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, user_id: int, token: str
) -> dict[str, Any]:
    api.clear()
    user = updates.user(user_id)
    await runtime.application.process_update(
        updates.command("/start", token, chat=updates.private(user), from_user=user)
    )
    (reply,) = api.calls_to("sendMessage")
    return reply


def _buttons(params: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """``{label: (button text, callback data)}`` of a menu."""
    result = {}
    for (button,) in _markup(params):
        result[button["text"].split(" ", 1)[1]] = (button["text"], button["callback_data"])
    return result


async def test_config_link_and_menu(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
) -> None:
    token = await _config_in_group(runtime, api, updates, group, ADMIN_ID)
    assert token is not None
    assert api.sent_texts() == ["功能设置"]

    menu = await _start(runtime, api, updates, ADMIN_ID, token)
    assert menu["text"] == "群名：My Group\n功能开关（点击切换）"
    assert _buttons(menu) == {
        "反垃圾": ("❌ 反垃圾", f"cfg:{CHAT}:antispam:1"),
        "入群验证": ("❌ 入群验证", f"cfg:{CHAT}:captcha:1"),
        "警告": ("❌ 警告", f"cfg:{CHAT}:warn:1"),
        "删除系统消息": ("✅ 删除系统消息", f"cfg:{CHAT}:delete_service_messages:0"),
    }
    # The link works once.
    again = await _start(runtime, api, updates, ADMIN_ID, token)
    assert again["text"] == "链接已失效或已被使用，请回到群里重新获取。"


@pytest.mark.parametrize("user_id", [WEAK_ADMIN_ID, MEMBER_ID])
async def test_config_in_group_needs_ban_rights(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
    user_id: int,
) -> None:
    before = metric_value("akkiabot_command_total", command="config", result="forbidden")
    assert await _config_in_group(runtime, api, updates, group, user_id) is None
    assert api.sent_texts() == ["需要群主或具有封禁成员权限的管理员才能使用 /config。"]
    after = metric_value("akkiabot_command_total", command="config", result="forbidden")
    assert after == before + 1


async def test_config_link_is_bound_to_its_user(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
) -> None:
    token = await _config_in_group(runtime, api, updates, group, OWNER_ID)
    assert token is not None
    reply = await _start(runtime, api, updates, ADMIN_ID, token)  # another admin
    assert reply["text"] == "验证失败：您不是发起配置的用户。"
    assert "reply_markup" not in reply


async def test_rights_are_rechecked_when_opening_the_menu(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
) -> None:
    token = await _config_in_group(runtime, api, updates, group, ADMIN_ID)
    assert token is not None
    members[ADMIN_ID] = UpdateFactory.chat_member(UpdateFactory.user(ADMIN_ID))  # demoted
    reply = await _start(runtime, api, updates, ADMIN_ID, token)
    assert reply["text"] == "需要群主或具有封禁成员权限的管理员才能使用 /config。"


async def test_config_in_private_chat_points_to_the_group(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    user = updates.user(OWNER_ID)
    await runtime.application.process_update(
        updates.command("/config", chat=updates.private(user), from_user=user)
    )
    assert api.sent_texts() == ["请在需要配置的群组中发送 /config 命令。"]


async def test_toggle_updates_settings_and_redraws_menu(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
) -> None:
    token = await _config_in_group(runtime, api, updates, group, ADMIN_ID)
    assert token is not None
    menu = await _start(runtime, api, updates, ADMIN_ID, token)
    _, data = _buttons(menu)["警告"]

    api.clear()
    await runtime.application.process_update(
        updates.callback_query(data, from_user=updates.user(ADMIN_ID))
    )
    assert (await runtime.deps.chat_settings.get(CHAT)).warn is True
    (edit,) = api.calls_to("editMessageText")
    assert _buttons(edit)["警告"] == ("✅ 警告", f"cfg:{CHAT}:warn:0")
    (answer,) = api.calls_to("answerCallbackQuery")
    assert answer["text"] == "设置成功"

    # Turning the service-message switch off is honoured by the group handler.
    _, data = _buttons(edit)["删除系统消息"]
    await runtime.application.process_update(
        updates.callback_query(data, from_user=updates.user(ADMIN_ID))
    )
    assert await runtime.deps.chat_settings.get(CHAT) == ChatSettings(
        warn=True, delete_service_messages=False
    )
    api.clear()
    await runtime.application.process_update(updates.service_message(chat=group))
    assert api.calls_to("deleteMessage") == []


@pytest.mark.parametrize("user_id", [WEAK_ADMIN_ID, MEMBER_ID, 999])
async def test_forged_or_unauthorised_callback_is_rejected(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
    user_id: int,
) -> None:
    # The chat is known (someone posted), but the presser may not configure it.
    await runtime.application.process_update(
        updates.message("hi", chat=group, from_user=updates.user(MEMBER_ID))
    )
    api.clear()
    await runtime.application.process_update(
        updates.callback_query(f"cfg:{CHAT}:antispam:1", from_user=updates.user(user_id))
    )
    assert (await runtime.deps.chat_settings.get(CHAT)).antispam is False
    assert api.calls_to("editMessageText") == []
    (answer,) = api.calls_to("answerCallbackQuery")
    assert answer["text"] == "需要群主或具有封禁成员权限的管理员才能修改设置"
    assert answer["show_alert"] in (True, "true")


async def test_callback_rechecks_rights_after_demotion(
    runtime: Runtime,
    api: FakeBotApi,
    updates: UpdateFactory,
    group: dict[str, Any],
    members: dict[int, dict[str, Any]],
) -> None:
    token = await _config_in_group(runtime, api, updates, group, ADMIN_ID)
    assert token is not None
    menu = await _start(runtime, api, updates, ADMIN_ID, token)
    members[ADMIN_ID] = UpdateFactory.chat_member(
        UpdateFactory.user(ADMIN_ID), "administrator", can_restrict_members=False
    )
    api.clear()
    await runtime.application.process_update(
        updates.callback_query(_buttons(menu)["反垃圾"][1], from_user=updates.user(ADMIN_ID))
    )
    assert (await runtime.deps.chat_settings.get(CHAT)).antispam is False
    assert api.calls_to("editMessageText") == []


@pytest.mark.parametrize(
    "data",
    [
        f"cfg:{CHAT}:max_warn_count:1",  # not a switch
        f"cfg:{CHAT}:warn:2",
        "cfg:abc:warn:1",
        "cfg:",
    ],
)
async def test_malformed_callback_data(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, data: str
) -> None:
    await runtime.application.process_update(
        updates.callback_query(data, from_user=updates.user(OWNER_ID))
    )
    (answer,) = api.calls_to("answerCallbackQuery")
    assert answer["text"] == "无效按钮"
    assert api.calls_to("getChatMember") == []


async def test_callback_for_unknown_chat(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, members: dict[int, dict[str, Any]]
) -> None:
    await runtime.application.process_update(
        updates.callback_query(
            f"cfg:{CHAT}:{Toggle.WARN.value}:1", from_user=updates.user(OWNER_ID)
        )
    )
    (answer,) = api.calls_to("answerCallbackQuery")
    assert answer["text"] == "获取群组信息失败，机器人可能不在群内。"


async def test_lookup_failure_in_group(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory, group: dict[str, Any]
) -> None:
    api.fail("getChatMember", "Bad Request: chat not found")
    assert await _config_in_group(runtime, api, updates, group, OWNER_ID) is None
    assert api.sent_texts() == ["获取群组信息失败，机器人可能不在群内。"]
