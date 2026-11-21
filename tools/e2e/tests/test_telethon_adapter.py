"""Pure conversion helpers of the Telethon adapter, on real Telethon objects (no network)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from telethon import errors, types

from akkiabot_e2e.errors import FloodWait
from akkiabot_e2e.telethon_transport import _guard, _msg, _role, _service_message_id
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


def test_inline_buttons() -> None:
    # Seen live in the test environment (2026-11-14): KeyboardInlineButton(type=InlineButtonType*).
    current = types.ReplyInlineMarkup(
        rows=[
            types.KeyboardInlineButtonRow(
                buttons=[
                    types.KeyboardInlineButton(
                        text="go", type=types.InlineButtonTypeUrl(url="https://t.me/b?start=x")
                    )
                ]
            ),
            types.KeyboardInlineButtonRow(
                buttons=[
                    types.KeyboardInlineButton(
                        text="✅ 警告", type=types.InlineButtonTypeCallback(data=b"cfg:-1:warn:0")
                    )
                ]
            ),
        ]
    )
    message = types.Message(
        id=1, peer_id=types.PeerUser(5), date=NOW, message="menu", reply_markup=current
    )
    msg = _msg(5, message)
    assert [(b.text, b.url, b.data) for b in msg.buttons] == [
        ("go", "https://t.me/b?start=x", None),
        ("✅ 警告", None, b"cfg:-1:warn:0"),
    ]
    assert msg.button("警告") == msg.buttons[1]
    assert (
        _msg(5, types.Message(id=2, peer_id=types.PeerUser(5), date=NOW, message="x")).buttons == ()
    )


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
    # messages.importChatInvite now answers ChatInviteJoinResultOk(updates=Updates)
    # (seen live in the test environment, 2026-11-14); unwrap it.
    assert _service_message_id(types.messages.ChatInviteJoinResultOk(updates=updates)) == 99
    assert (
        _service_message_id(types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)) is None
    )
    assert _msg(-1001234, service).service == "MessageActionChatJoinedByLink"


async def test_flood_wait_is_translated() -> None:
    async def flood() -> None:
        raise errors.FloodWaitError(request=None, capture=17)

    with pytest.raises(FloodWait) as info:
        await _guard(flood())
    assert info.value.seconds == 17


def test_web_app_button_is_marked() -> None:
    markup = types.ReplyInlineMarkup(
        rows=[
            types.KeyboardInlineButtonRow(
                buttons=[
                    types.KeyboardInlineButton(
                        text="开始验证",
                        type=types.InlineButtonTypeWebView(
                            url="https://w.example/captcha?challenge=x"
                        ),
                    )
                ]
            )
        ]
    )
    message = types.Message(
        id=1, peer_id=types.PeerUser(5), date=NOW, message="go", reply_markup=markup
    )
    (button,) = _msg(5, message).buttons
    assert (button.url, button.web_app) == ("https://w.example/captcha?challenge=x", True)


def _banned(*, left: bool, view_messages: bool) -> types.ChannelParticipantBanned:
    return types.ChannelParticipantBanned(
        peer=types.PeerUser(777),
        kicked_by=1,
        date=NOW,
        banned_rights=types.ChatBannedRights(
            until_date=None, view_messages=view_messages, send_messages=True
        ),
        left=left,
    )


def test_muted_member_is_still_a_member() -> None:
    # A spam mute arriving late made restore_member think the member had left
    # and try to add it again (seen in a staging release acceptance run).
    assert _role(_banned(left=False, view_messages=False)) == "member"
    assert _role(_banned(left=False, view_messages=True)) is None  # banned
    assert _role(_banned(left=True, view_messages=False)) is None  # muted, then left
    assert _role(types.ChannelParticipant(user_id=777, date=NOW)) == "member"
    assert _role(None) is None
