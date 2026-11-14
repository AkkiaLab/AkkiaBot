"""Pure conversion helpers of the Telethon adapter, on real Telethon objects (no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon import errors, types

from akkiabot_e2e.errors import FloodWait
from akkiabot_e2e.telethon_transport import _guard, _msg, _service_message_id
from akkiabot_e2e.transport import code_spans

NOW = datetime(2026, 11, 14, tzinfo=UTC)


def test_message_conversion_keeps_reply_and_code_entities() -> None:
    text = "当前ChatId: 测试群 (-1001234)"
    message = types.Message(
        id=42,
        peer_id=types.PeerChannel(1234),
        date=NOW,
        message=text,
        from_id=types.PeerUser(777),
        reply_to=types.MessageReplyHeader(reply_to_msg_id=41),
        entities=[types.MessageEntityCode(offset=text.index("-"), length=8)],
    )

    msg = _msg(-1001234, message)

    assert (msg.id, msg.sender_id, msg.reply_to_id, msg.service) == (42, 777, 41, None)
    assert code_spans(msg) == ["-1001234"]


def test_service_message_id_from_updates() -> None:
    service = types.MessageService(
        id=99,
        peer_id=types.PeerChannel(1234),
        date=NOW,
        action=types.MessageActionChatJoinedByLink(inviter_id=1),
        from_id=types.PeerUser(777),
    )
    updates = types.Updates(
        updates=[types.UpdateNewChannelMessage(message=service, pts=1, pts_count=1)],
        users=[],
        chats=[],
        date=NOW,
        seq=0,
    )

    assert _service_message_id(updates) == 99
    assert (
        _service_message_id(types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)) is None
    )
    assert _msg(-1001234, service).service == "MessageActionChatJoinedByLink"


def test_service_message_id_unwraps_nested_updates() -> None:
    # messages.importChatInvite now returns ChatInviteJoinResultOk(updates=Updates(...)).
    service = types.MessageService(
        id=7,
        peer_id=types.PeerChannel(1234),
        date=NOW,
        action=types.MessageActionChatJoinedByLink(inviter_id=1),
        from_id=types.PeerUser(777),
    )
    inner = types.Updates(
        updates=[types.UpdateNewChannelMessage(message=service, pts=1, pts_count=1)],
        users=[],
        chats=[],
        date=NOW,
        seq=0,
    )

    assert _service_message_id(SimpleNamespace(updates=inner)) == 7
    assert _service_message_id(SimpleNamespace(updates=SimpleNamespace(updates=inner))) == 7
    assert _service_message_id(SimpleNamespace(updates=None)) is None


async def test_flood_wait_is_translated() -> None:
    async def flood() -> None:
        raise errors.FloodWaitError(request=None, capture=17)

    with pytest.raises(FloodWait) as info:
        await _guard(flood())
    assert info.value.seconds == 17
