from collections.abc import Callable

import pytest
from telegram import Update

from akkiabot_server.bot.commands.basic import START_GREETING, START_INVALID
from akkiabot_server.bot.types import Context
from akkiabot_server.runtime import Runtime
from akkiabot_server.services.start_payload import StartPayload
from akkiabot_server.testing import BOT_USERNAME, FakeBotApi, UpdateFactory, metric_value

GROUP_ID = -1001234567890


async def test_help_lists_commands(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    await runtime.application.process_update(updates.command("/help"))
    assert api.sent_texts() == [
        "可用命令列表：\n"
        "1. 获取帮助信息\n用法：/help [命令名]\n"
        "2. 获取ID信息\n用法：/id（回复一条消息时，同时显示被回复者的 ID）"
    ]


async def test_help_for_one_command(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    await runtime.application.process_update(updates.command("/help", "id"))
    await runtime.application.process_update(updates.command("/help", "nope"))
    assert api.sent_texts() == [
        "用法：/id（回复一条消息时，同时显示被回复者的 ID）",
        "命令 nope 不存在",
    ]


async def test_command_addressed_to_this_bot(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    await runtime.application.process_update(updates.command(f"/help@{BOT_USERNAME}"))
    await runtime.application.process_update(updates.command("/help@other_bot"))
    assert len(api.sent_texts()) == 1


async def test_id_in_group(runtime: Runtime, api: FakeBotApi, updates: UpdateFactory) -> None:
    update = updates.command(
        "/id",
        chat=updates.group(GROUP_ID, "Test_Group (1)"),
        from_user=updates.user(42, "Al.ice"),
    )
    await runtime.application.process_update(update)

    (sent,) = api.calls_to("sendMessage")
    assert sent["parse_mode"] == "MarkdownV2"
    assert sent["chat_id"] == GROUP_ID
    assert sent["reply_parameters"]["message_id"] == update.effective_message.message_id  # type: ignore[union-attr]
    assert sent["text"] == (
        f"当前ChatId: Test\\_Group \\(1\\) \\(`{GROUP_ID}`\\)\n你的UserId: Al\\.ice \\(`42`\\)"
    )


async def test_id_replying_to_someone(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    group = updates.group(GROUP_ID, "G")
    replied = updates.message_dict(chat=group, from_user=updates.user(7, "Bob"), text="hi")
    await runtime.application.process_update(
        updates.command("/id", chat=group, from_user=updates.user(42, "Alice"), reply_to=replied)
    )
    assert api.sent_texts()[0].splitlines()[-1] == "回复目标的UserId: Bob \\(`7`\\)"


async def test_id_replying_to_channel_post(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    group = updates.group(GROUP_ID, "G")
    channel = {"id": -1009876543210, "type": "channel", "title": "News"}
    replied = updates.message_dict(
        chat=group,
        from_user=updates.user(136817688, "Channel", is_bot=True),
        sender_chat=channel,
        text="post",
    )
    await runtime.application.process_update(updates.command("/id", chat=group, reply_to=replied))
    assert api.sent_texts()[0].splitlines()[-1] == "回复目标的ChatId: News \\(`-1009876543210`\\)"


async def test_id_ignores_implicit_topic_reply(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    group = updates.group(GROUP_ID, "Forum")
    topic_header = updates.message_dict(
        chat=group,
        from_user=updates.user(1, "Alice"),
        forum_topic_created={"name": "topic", "icon_color": 7322096},
    )
    await runtime.application.process_update(
        updates.command("/id", chat=group, reply_to=topic_header, is_topic_message=True)
    )
    assert len(api.sent_texts()[0].splitlines()) == 2


async def test_id_in_private_chat(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    alice = updates.user(42, "Alice")
    await runtime.application.process_update(
        updates.command("/id", chat=updates.private(alice), from_user=alice)
    )
    assert api.sent_texts() == ["当前ChatId: Alice \\(`42`\\)\n你的UserId: Alice \\(`42`\\)"]


# ------------------------------------------------------------------- /start


@pytest.fixture
def private_command(updates: UpdateFactory) -> Callable[..., Update]:
    alice = updates.user(42, "Alice")

    def make(*args: str) -> Update:
        return updates.command("/start", *args, chat=updates.private(alice), from_user=alice)

    return make


async def test_start_without_payload_greets(
    runtime: Runtime, api: FakeBotApi, private_command: Callable[..., Update]
) -> None:
    await runtime.application.process_update(private_command())
    assert api.sent_texts() == [START_GREETING]


async def test_start_ignored_in_groups(
    runtime: Runtime, api: FakeBotApi, updates: UpdateFactory
) -> None:
    await runtime.application.process_update(updates.command("/start"))
    assert api.sent_texts() == []


async def test_start_dispatches_payload_once(
    runtime: Runtime, api: FakeBotApi, private_command: Callable[..., Update]
) -> None:
    seen: list[StartPayload] = []

    async def on_demo(update: Update, _context: Context, payload: StartPayload) -> None:
        seen.append(payload)
        assert update.effective_message is not None
        await update.effective_message.reply_text("demo ok")

    runtime.deps.on_start("demo", on_demo)
    token = await runtime.deps.start_payloads.create("demo", {"chat_id": GROUP_ID})
    before_ok = metric_value("akkiabot_command_total", command="start", result="ok")
    before_bad = metric_value("akkiabot_command_total", command="start", result="invalid_payload")

    await runtime.application.process_update(private_command(token))
    await runtime.application.process_update(private_command(token))

    assert seen == [StartPayload(kind="demo", data={"chat_id": GROUP_ID})]
    assert api.sent_texts() == ["demo ok", START_INVALID]
    assert metric_value("akkiabot_command_total", command="start", result="ok") == before_ok + 1
    assert (
        metric_value("akkiabot_command_total", command="start", result="invalid_payload")
        == before_bad + 1
    )


async def test_start_rejects_unknown_kind(
    runtime: Runtime, api: FakeBotApi, private_command: Callable[..., Update]
) -> None:
    token = await runtime.deps.start_payloads.create("unregistered")
    await runtime.application.process_update(private_command(token))
    assert api.sent_texts() == [START_INVALID]


async def test_start_rejects_garbage_token(
    runtime: Runtime, api: FakeBotApi, private_command: Callable[..., Update]
) -> None:
    await runtime.application.process_update(private_command("not a token!"))
    assert api.sent_texts() == [START_INVALID]


async def test_duplicate_start_kind_rejected(runtime: Runtime) -> None:
    async def handler(_u: Update, _c: Context, _p: StartPayload) -> None:
        return None

    runtime.deps.on_start("demo", handler)
    with pytest.raises(ValueError, match="重复注册"):
        runtime.deps.on_start("demo", handler)
