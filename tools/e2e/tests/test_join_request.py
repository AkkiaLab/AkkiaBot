"""BOT-33: m2.join-request against the fake bot (pending request → private notice)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from telethon import errors, functions, types

from akkiabot_e2e.config import E2EConfig
from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import JOIN_REQUEST_TEXT, select_scenarios
from akkiabot_e2e.telethon_transport import TelethonAccount
from akkiabot_e2e.testing import (
    ADMIN,
    BOT,
    MEMBER,
    Connector,
    FakeRegistry,
    FakeTelegram,
    FakeTraces,
    FakeWeb,
)
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS


async def _no_sleep(_: float) -> None:
    return None


async def _run(world: FakeTelegram, config: E2EConfig) -> Report:
    return await run_acceptance(
        config,
        select_scenarios(["m2.join-request"]),
        connect=Connector(world),
        run_id="test-run",
        allow_create=True,
        pacer=Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        traces=FakeTraces(),
        registry=FakeRegistry(),
        web=FakeWeb(world),
    )


def _group(world: FakeTelegram):
    (chat,) = [c for c in world.chats.values() if c.id < 0]
    return chat


def _result(report: Report):
    (result,) = [r for r in report.results if r.name == "m2.join-request"]
    return result


def test_notice_text_is_the_fixed_text() -> None:
    assert JOIN_REQUEST_TEXT == "Hello, please wait for the admin to approve your request."


def test_bot_needs_invite_users_to_see_join_requests() -> None:
    assert "invite_users" in BOT_ADMIN_RIGHTS


async def test_pending_request_gets_private_notice_then_member_is_restored(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await _run(world, config)

    assert report.status == "passed", report.to_markdown(Redactor())
    steps = [e["step"] for e in _result(report).evidence]
    assert steps[-3:] == ["检查成员是否仍在申请阶段", "群主拒绝入群申请", "群主把成员拉回群"]
    notice = next(e for e in _result(report).evidence if e["step"] == "bot 私聊等待提示")
    assert (notice["sender_id"], notice["text"]) == (BOT.id, JOIN_REQUEST_TEXT)
    chat = _group(world)
    assert chat.members[MEMBER.id] == "member"
    assert chat.join_requests == set()
    # Asking is not a link join; the member came back through the owner.
    assert "join_by_link" not in world.calls
    assert report.joins == {
        "member_join_by_link": 0,
        "member_join_request": 1,
        "owner_added_member": 2,
    }
    assert "成员通过需审批的链接申请入群 1 次" in report.to_markdown(Redactor())


async def test_an_older_notice_in_the_private_chat_does_not_count(
    world: FakeTelegram, config: E2EConfig
) -> None:
    world.add_bot(BOT, join_request_silent=True)
    world.post(world.private_chat(BOT.id, MEMBER.id), BOT.id, JOIN_REQUEST_TEXT)

    report = await _run(world, replace(config, reply_timeout=0.05))

    assert report.status == "failed"
    assert "没有收到 bot 的私聊等待提示" in _result(report).detail
    assert _group(world).members[MEMBER.id] == "member"  # restored even on failure


@pytest.mark.parametrize(
    ("knob", "message"),
    [
        ("join_request_silent", "没有收到 bot 的私聊等待提示"),
        ("join_request_approves", "bot 不应自行批准申请"),
    ],
)
async def test_regressions_fail(
    world: FakeTelegram, config: E2EConfig, knob: str, message: str
) -> None:
    world.add_bot(BOT, **{knob: True})

    report = await _run(world, replace(config, reply_timeout=0.05))

    assert report.status == "failed"
    assert message in _result(report).detail
    chat = _group(world)
    assert chat.members[MEMBER.id] == "member"
    assert chat.join_requests == set()


async def test_preparation_grants_the_invite_right_on_an_existing_group(
    world: FakeTelegram, config: E2EConfig
) -> None:
    # A fixed group prepared before BOT-33: the bot lacks "invite users".
    chat = world.add_chat(config.chat_title, ADMIN)
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.rights[BOT.id] = frozenset({"delete_messages", "ban_users"})
    config = replace(config, allowed_chat_ids=frozenset({chat.id}), reply_timeout=0.05)

    report = await _run(world, config)

    # Preparation grants the missing right, so the request reaches the bot.
    assert report.status == "passed", report.to_markdown(Redactor())
    assert "invite_users" in chat.rights[BOT.id]


class _StubClient:
    """Just enough of ``TelegramClient`` for the join request calls."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.requests: list[object] = []

    async def get_input_entity(self, peer: int) -> types.InputPeerUser:
        return types.InputPeerUser(abs(peer), 0)

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return types.Updates(updates=[], users=[], chats=[], date=None, seq=0)


async def test_telethon_request_join_expects_a_pending_request() -> None:
    sent = _StubClient(errors.InviteRequestSentError(request=None))
    await TelethonAccount(sent).request_join("https://t.me/+abcdef")  # pyright: ignore[reportArgumentType]
    (request,) = sent.requests
    assert isinstance(request, functions.messages.ImportChatInviteRequest)
    assert request.hash == "abcdef"

    joined = _StubClient(None)  # Telegram let the account in: no approval needed
    with pytest.raises(E2EError, match="没有要求管理员审批"):
        await TelethonAccount(joined).request_join("https://t.me/+abcdef")  # pyright: ignore[reportArgumentType]


async def test_telethon_decline_is_idempotent() -> None:
    client = _StubClient(errors.HideRequesterMissingError(request=None))
    await TelethonAccount(client).decline_join_request(-1001234, MEMBER.id)  # pyright: ignore[reportArgumentType]
    (request,) = client.requests
    assert isinstance(request, functions.messages.HideChatJoinRequestRequest)
    assert request.approved is False
