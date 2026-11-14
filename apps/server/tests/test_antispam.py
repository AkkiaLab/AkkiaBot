"""Group spam checks and ``/spam`` through the real PTB application,
a fake Bot API and a fake OpenAI-compatible endpoint."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from akkiabot_antispam.testing import FakeLlm, RecordingTracer, completion, error, verdict_json
from akkiabot_core.cache import MemoryCache
from akkiabot_core.db import ChatMemberRepository, MessageLogRepository, Restriction, session_scope
from akkiabot_core.db.migrate import upgrade_engine
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.chat_settings import Toggle
from akkiabot_server.services.llm import build_llm
from akkiabot_server.testing import (
    BOT_ID,
    BOT_USER,
    FakeBotApi,
    UpdateFactory,
    member_responder,
    metric_value,
)

from .conftest import MakeSettings

CHAT = -1001234567890
OWNER_ID, MEMBER_ID, SPAMMER_ID, ADMIN_ID = 1, 4, 42, 2


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def llm() -> FakeLlm:
    return FakeLlm()


@pytest.fixture
def tracer() -> RecordingTracer:
    return RecordingTracer()


@pytest.fixture
async def runtime(
    make_settings: MakeSettings, api: FakeBotApi, llm: FakeLlm, tracer: RecordingTracer
) -> AsyncIterator[Runtime]:
    """Runtime with an LLM configured (FakeLlm) and no real delays."""
    settings = make_settings(llm={"model": "test-model"})
    rt = Runtime.build(
        settings,
        cache=MemoryCache(),
        request=api,
        get_updates_request=api,
        llm=build_llm(settings, client=llm.client(), tracer=tracer),
    )
    rt.deps.antispam.sleep = _no_sleep
    await upgrade_engine(rt.engine)
    await rt.application.initialize()
    try:
        yield rt
    finally:
        await rt.deps.background.drain()
        await rt.deps.background.aclose()
        await rt.application.shutdown()
        await rt.llm.aclose()
        await rt.http.aclose()
        await rt.engine.dispose()


@pytest.fixture
def members(api: FakeBotApi) -> dict[int, dict[str, Any]]:
    table = {
        OWNER_ID: UpdateFactory.chat_member(UpdateFactory.user(OWNER_ID, "Owner"), "creator"),
        ADMIN_ID: UpdateFactory.chat_member(
            UpdateFactory.user(ADMIN_ID, "Ad_min"), "administrator"
        ),
        MEMBER_ID: UpdateFactory.chat_member(UpdateFactory.user(MEMBER_ID, "Member")),
        SPAMMER_ID: UpdateFactory.chat_member(UpdateFactory.user(SPAMMER_ID, "Spam*Bot")),
    }
    api.respond("getChatMember", member_responder(table))
    return table


class Group:
    def __init__(self, runtime: Runtime, updates: UpdateFactory) -> None:
        self.runtime = runtime
        self.updates = updates
        self.chat = updates.group(CHAT, "Group")

    def user(self, user_id: int) -> dict[str, Any]:
        names = {OWNER_ID: "Owner", MEMBER_ID: "Member", SPAMMER_ID: "Spam*Bot", ADMIN_ID: "Ad_min"}
        return self.updates.user(user_id, names.get(user_id, "User"))

    async def say(self, user_id: int, text: str | None = "hello", **fields: Any) -> dict[str, Any]:
        update = self.updates.message(text, chat=self.chat, from_user=self.user(user_id), **fields)
        await self.process(update)
        assert update.message is not None
        return update.message.to_dict()

    async def process(self, update: Any) -> None:
        await self.runtime.application.process_update(update)
        await self.runtime.deps.background.drain()

    async def enable(self) -> None:
        # Any message makes the chat known (the first one is not checked: switch off).
        await self.say(OWNER_ID, "hi")
        await self.runtime.deps.chat_settings.set_toggle(CHAT, Toggle.ANTISPAM, True)


@pytest.fixture
def group(runtime: Runtime, updates: UpdateFactory) -> Group:
    return Group(runtime, updates)


def deleted_ids(api: FakeBotApi) -> list[int]:
    ids: list[int] = []
    for params in api.calls_to("deleteMessages"):
        raw = params["message_ids"]
        ids += json.loads(raw) if isinstance(raw, str) else raw
    return sorted(ids)


def verdicts(action: str, verdict: str) -> float:
    return metric_value("akkiabot_antispam_verdict_total", verdict=verdict, action=action)


# ------------------------------------------------------------- group messages


async def test_new_member_spam_is_removed(
    runtime: Runtime,
    api: FakeBotApi,
    llm: FakeLlm,
    tracer: RecordingTracer,
    group: Group,
    members: dict[int, dict[str, Any]],
) -> None:
    await group.enable()
    earlier = await group.say(SPAMMER_ID, "大家好")  # checked, not spam (default answer 0)
    api.clear()
    removed = verdicts("removed", "spam")
    llm.queue(verdict_json(True, "gambling", 0.97))

    spam = await group.say(SPAMMER_ID, "注册即送888彩金，提款秒到")

    assert llm.last()["messages"][1]["content"] == "注册即送888彩金，提款秒到"
    # The message, then (delayed) the logged history, then (delayed) the notice.
    assert deleted_ids(api) == sorted(
        [spam["message_id"], earlier["message_id"], spam["message_id"], 10_000]
    )
    (restrict,) = api.calls_to("restrictChatMember")
    assert int(restrict["user_id"]) == SPAMMER_ID
    permissions = restrict["permissions"]
    permissions = json.loads(permissions) if isinstance(permissions, str) else permissions
    assert not any(permissions.values())
    notice = api.calls_to("sendMessage")[0]
    assert notice["parse_mode"] == "MarkdownV2"
    assert notice["text"] == (
        "\\[Anti SPAM\\]\n内容命中反垃圾策略\n用户："
        f"[Spam\\*Bot](tg://user?id={SPAMMER_ID})\n动作：删除消息、禁言"
    )
    async with session_scope(runtime.session_factory) as session:
        row = await MessageLogRepository(session).get(CHAT, spam["message_id"])
        assert row is not None
        assert row.text == "注册即送888彩金，提款秒到"  # kept for auditing
        assert row.spam_verdict is not None
        assert row.spam_verdict["is_spam"] is True
        assert row.spam_verdict["category"] == "gambling"
        assert row.spam_verdict["prompt_version"] == "antispam-v1"
        member = await ChatMemberRepository(session).get(CHAT, SPAMMER_ID)
        assert member is not None
        assert member.restriction is Restriction.SPAM_MUTE
        # The ham message's row was purged with the history.
        assert await MessageLogRepository(session).get(CHAT, earlier["message_id"]) is None
    assert verdicts("removed", "spam") == removed + 1
    # Every classification is traced (Langfuse): the ham message and the spam one.
    assert len(tracer.generations) == 2
    assert tracer.generations[-1]["start"]["prompt_version"] == "antispam-v1"
    assert tracer.generations[-1]["end"]["usage"].total_tokens == 128


async def test_check_does_not_block_update_processing(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    await group.enable()
    llm.queue(verdict_json(True))
    update = group.updates.message("buy now", chat=group.chat, from_user=group.user(SPAMMER_ID))
    await runtime.application.process_update(update)
    # The handler only scheduled the check; nothing was classified yet.
    assert len(runtime.deps.background) == 1
    assert llm.requests == []
    await runtime.deps.background.drain()
    assert len(llm.requests) == 1


async def test_disabled_group_is_not_checked(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    await group.say(SPAMMER_ID, "buy now")
    await runtime.deps.chat_settings.set_toggle(CHAT, Toggle.ANTISPAM, True)
    await group.say(SPAMMER_ID, "buy now")
    await runtime.deps.chat_settings.set_toggle(CHAT, Toggle.ANTISPAM, False)
    await group.say(SPAMMER_ID, "buy now")
    assert len(llm.requests) == 1  # only while the switch was on
    assert len(runtime.deps.background) == 0


async def test_members_with_more_than_n_messages_are_exempt(
    runtime: Runtime, llm: FakeLlm, group: Group
) -> None:
    await group.enable()
    exempt = verdicts("exempt", "skipped")
    for i in range(5):
        await group.say(MEMBER_ID, f"message {i}")
    # Messages 1-3 are checked; from the 4th on the member has more than 3.
    assert len(llm.requests) == 3
    assert verdicts("exempt", "skipped") == exempt + 2


async def test_ham_verdict_is_stored_without_text(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    await group.enable()
    llm.queue(verdict_json(False))
    ham = verdicts("none", "ham")
    message = await group.say(MEMBER_ID, "今天天气真好啊")
    assert api.calls_to("deleteMessages") == []
    assert api.calls_to("restrictChatMember") == []
    async with session_scope(runtime.session_factory) as session:
        row = await MessageLogRepository(session).get(CHAT, message["message_id"])
    assert row is not None
    assert row.spam_verdict is not None
    assert row.spam_verdict["is_spam"] is False
    assert row.text is None
    assert verdicts("none", "ham") == ham + 1


async def test_llm_failure_fails_open(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    await group.enable()
    llm.queue(error(500, "upstream down"))
    errors = metric_value("akkiabot_antispam_llm_errors_total", error="api_error")
    failed = verdicts("none", "error")
    await group.say(SPAMMER_ID, "注册即送888彩金")
    assert api.calls_to("deleteMessages") == []
    assert api.calls_to("restrictChatMember") == []
    assert metric_value("akkiabot_antispam_llm_errors_total", error="api_error") == errors + 1
    assert verdicts("none", "error") == failed + 1


async def test_structured_output_fallback_through_the_bot(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group, members: dict[int, Any]
) -> None:
    await group.enable()
    llm.queue(error(400, "response_format not supported"), completion("1"))
    await group.say(SPAMMER_ID, "看我主页日入2W")
    assert "response_format" not in llm.last()
    assert len(api.calls_to("restrictChatMember")) == 1


async def test_admin_spam_is_not_acted_upon(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group, members: dict[int, Any]
) -> None:
    await group.enable()
    llm.queue(verdict_json(True))
    admin = verdicts("admin", "spam")
    await group.say(ADMIN_ID, "招聘兼职，日结300+")
    assert api.calls_to("deleteMessages") == []
    assert api.calls_to("restrictChatMember") == []
    assert verdicts("admin", "spam") == admin + 1


async def test_media_caption_is_checked(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group, members: dict[int, Any]
) -> None:
    await group.enable()
    llm.queue(verdict_json(True))
    photo = [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}]
    await group.say(SPAMMER_ID, None, photo=photo, caption="加我拉你进福利群")
    assert llm.last()["messages"][1]["content"] == "加我拉你进福利群"
    assert len(api.calls_to("restrictChatMember")) == 1


async def test_channels_bots_and_empty_messages_are_skipped(
    runtime: Runtime, llm: FakeLlm, group: Group
) -> None:
    await group.enable()
    channel = {"id": -100777, "type": "channel", "title": "Ch"}
    await group.process(
        group.updates.message(
            "buy now", chat=group.chat, from_user=group.user(SPAMMER_ID), sender_chat=channel
        )
    )
    await group.process(group.updates.message("buy now", chat=group.chat, from_user=BOT_USER))
    sticker = {
        "file_id": "s",
        "file_unique_id": "s",
        "width": 1,
        "height": 1,
        "is_animated": False,
        "is_video": False,
        "type": "regular",
    }
    await group.say(SPAMMER_ID, None, sticker=sticker)
    await group.say(SPAMMER_ID, "/help")  # commands are not classified
    assert llm.requests == []


async def test_redelivered_message_is_checked_once(
    runtime: Runtime, llm: FakeLlm, group: Group
) -> None:
    await group.enable()
    update = group.updates.message("hello", chat=group.chat, from_user=group.user(MEMBER_ID))
    await group.process(update)
    await group.process(update)
    assert len(llm.requests) == 1


async def test_missing_rights_are_reported(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group, members: dict[int, Any]
) -> None:
    await group.enable()
    api.fail("restrictChatMember", "Bad Request: not enough rights")
    api.fail("deleteMessages", "Bad Request: message can't be deleted")
    llm.queue(verdict_json(True))
    failed = verdicts("failed", "spam")
    await group.say(SPAMMER_ID, "buy now")
    notice = api.sent_texts()[0]
    assert "动作：删除消息失败、禁言失败" in notice
    assert "请确认机器人具有删除消息和封禁成员权限" in notice
    assert verdicts("failed", "spam") == failed + 1
    async with session_scope(runtime.session_factory) as session:
        member = await ChatMemberRepository(session).get(CHAT, SPAMMER_ID)
    assert member is not None
    assert member.restriction is Restriction.NONE


async def test_without_llm_nothing_is_classified(
    make_settings: MakeSettings, api: FakeBotApi
) -> None:
    rt = Runtime.build(make_settings(), cache=MemoryCache(), request=api, get_updates_request=api)
    assert rt.deps.antispam.classifier is None
    await rt.llm.aclose()
    await rt.http.aclose()
    await rt.engine.dispose()


# ----------------------------------------------------------------------- /spam


@pytest.fixture
def profiles(api: FakeBotApi) -> None:
    def get_chat(params: dict[str, Any]) -> dict[str, Any]:
        user_id = int(params["chat_id"])
        return {
            "id": user_id,
            "type": "private",
            "first_name": "Spam*Bot",
            "bio": "日结兼职，私聊",
            "accent_color_id": 0,
            "max_reaction_count": 0,
            "accepted_gift_types": {
                "unlimited_gifts": False,
                "limited_gifts": False,
                "unique_gifts": False,
                "premium_subscription": False,
                "gifts_from_channels": False,
            },
        }

    api.respond("getChat", get_chat)
    api.respond(
        "getChatAdministrators",
        lambda _params: [
            UpdateFactory.chat_member(UpdateFactory.user(OWNER_ID, "Owner"), "creator"),
            UpdateFactory.chat_member(UpdateFactory.user(ADMIN_ID, "Ad_min"), "administrator"),
            UpdateFactory.chat_member(BOT_USER, "administrator"),
        ],
    )


async def _report(group: Group, by: int, target: dict[str, Any]) -> None:
    await group.process(
        group.updates.message("/spam", chat=group.chat, from_user=group.user(by), reply_to=target)
    )


@pytest.mark.usefixtures("members", "profiles")
async def test_admin_report_bans_directly(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    target = await group.say(SPAMMER_ID, "buy now")
    api.clear()
    await _report(group, OWNER_ID, target)
    assert [int(c["user_id"]) for c in api.calls_to("banChatMember")] == [SPAMMER_ID]
    assert api.sent_texts()[-1] == (
        f"[Spam\\*Bot](tg://user?id={SPAMMER_ID}) 已被封禁（垃圾信息），删除了 1 条消息。"
    )
    assert llm.requests == []
    assert metric_value("akkiabot_command_total", command="spam", result="admin_ban") >= 1


@pytest.mark.usefixtures("members", "profiles")
async def test_member_report_judged_spam_bans(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, tracer: RecordingTracer, group: Group
) -> None:
    target = await group.say(SPAMMER_ID, "看我主页日入2W")
    api.clear()
    llm.queue(verdict_json(True, "ad"))
    banned = verdicts("report_ban", "spam")

    await _report(group, MEMBER_ID, target)

    assert api.sent_texts() == ["举报已收到，正在进行机器审核……"]
    assert llm.last()["messages"][1]["content"] == (
        "发送者名字: Spam*Bot\n发送者简介: 日结兼职，私聊\n消息内容: 看我主页日入2W"
    )
    assert [int(c["user_id"]) for c in api.calls_to("banChatMember")] == [SPAMMER_ID]
    (edit,) = api.calls_to("editMessageText")
    assert edit["parse_mode"] == "MarkdownV2"
    assert edit["text"] == (
        f"举报成立：判定为垃圾信息，已封禁 [Spam\\*Bot](tg://user?id={SPAMMER_ID})，"
        "删除了 1 条消息。"
    )
    async with session_scope(runtime.session_factory) as session:
        member = await ChatMemberRepository(session).get(CHAT, SPAMMER_ID)
        assert member is not None
        assert (member.restriction, member.restricted_by) == (Restriction.ADMIN_BAN, BOT_ID)
        row = await MessageLogRepository(session).get(CHAT, target["message_id"])
        assert row is not None
        assert row.text == "看我主页日入2W"  # audit row survives the purge
    assert verdicts("report_ban", "spam") == banned + 1
    assert tracer.generations[-1]["start"]["input"][1]["content"].startswith("发送者名字")


@pytest.mark.usefixtures("members", "profiles")
async def test_member_report_judged_ham_calls_admins(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    target = await group.say(SPAMMER_ID, "今天天气真好啊")
    api.clear()
    llm.queue(verdict_json(False))
    await _report(group, MEMBER_ID, target)
    assert api.calls_to("banChatMember") == []
    (edit,) = api.calls_to("editMessageText")
    assert edit["text"] == (
        "未判定为垃圾信息，请管理员人工审核被举报的消息。\n"
        f"[Owner](tg://user?id={OWNER_ID}) [Ad\\_min](tg://user?id={ADMIN_ID})"
    )


@pytest.mark.usefixtures("members", "profiles")
async def test_report_without_llm_calls_admins(
    make_settings: MakeSettings, api: FakeBotApi, updates: UpdateFactory
) -> None:
    rt = Runtime.build(make_settings(), cache=MemoryCache(), request=api, get_updates_request=api)
    await upgrade_engine(rt.engine)
    await rt.application.initialize()
    try:
        group = Group(rt, UpdateFactory(rt.application.bot))
        target = await group.say(SPAMMER_ID, "buy now")
        await _report(group, MEMBER_ID, target)
        (edit,) = api.calls_to("editMessageText")
        assert edit["text"].startswith("暂时无法自动审核，请管理员人工审核被举报的消息。")
        assert api.calls_to("banChatMember") == []
    finally:
        await rt.deps.background.aclose()
        await rt.application.shutdown()
        await rt.http.aclose()
        await rt.engine.dispose()


@pytest.mark.usefixtures("members", "profiles")
async def test_report_rules(runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group) -> None:
    admin_message = await group.say(ADMIN_ID, "hello")
    own = await group.say(MEMBER_ID, "hello")
    target = await group.say(SPAMMER_ID, "hello")
    api.clear()
    await _report(group, MEMBER_ID, admin_message)
    await _report(group, MEMBER_ID, own)
    for _ in range(4):  # default limit: 3 reports per 5 minutes
        await _report(group, MEMBER_ID, target)
    texts = api.sent_texts()
    assert texts[0] == "无法举报管理员或群主。"
    assert texts[1] == "不能举报自己。"
    assert texts[2:5] == ["举报已收到，正在进行机器审核……"] * 3
    assert texts[5].startswith("举报过于频繁，请 ")
    assert len(llm.requests) == 3


@pytest.mark.usefixtures("members", "profiles")
async def test_report_requires_reply(runtime: Runtime, api: FakeBotApi, group: Group) -> None:
    await group.process(
        group.updates.message("/spam", chat=group.chat, from_user=group.user(MEMBER_ID))
    )
    assert api.sent_texts() == [
        "此命令需要以回复的方式使用，将举报被回复的成员。\n用法：/spam（回复要举报的消息）"
    ]


@pytest.mark.usefixtures("members", "profiles")
async def test_report_without_bio_is_still_judged(
    runtime: Runtime, api: FakeBotApi, llm: FakeLlm, group: Group
) -> None:
    api.fail("getChat", "Bad Request: chat not found")
    target = await group.say(SPAMMER_ID, "buy now")
    llm.queue(verdict_json(False))
    await _report(group, MEMBER_ID, target)
    assert llm.last()["messages"][1]["content"] == "发送者名字: Spam*Bot\n消息内容: buy now"
    assert len(api.calls_to("editMessageText")) == 1
