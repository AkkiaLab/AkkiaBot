"""Telethon (MTProto) implementation of :class:`~akkiabot_e2e.transport.Account`.

Only this module imports Telethon. Sessions are ``StringSession`` values held
in memory (from 1Password); nothing is written to disk here. Telethon's own
flood sleeping is disabled so :class:`~akkiabot_e2e.pacing.Pacer` decides.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from akkiabot_e2e.config import ApiCredentials
from akkiabot_e2e.errors import E2EError, FloodWait, SafetyError
from akkiabot_e2e.login import Ask
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.transport import Button, ChatRef, Entity, Msg, Role, UserInfo

#: Telethon logs connection details at INFO/DEBUG; keep only warnings, whatever the root level.
THIRD_PARTY_LOGGERS = ("telethon",)

_ENTITY_KINDS = {
    types.MessageEntityCode: "code",
    types.MessageEntityPre: "pre",
    types.MessageEntityBold: "bold",
    types.MessageEntityItalic: "italic",
    types.MessageEntityTextUrl: "text_link",
    types.MessageEntityUrl: "url",
    types.MessageEntityBotCommand: "bot_command",
    types.MessageEntityMentionName: "mention_name",
}
_INVITE_TTL = timedelta(hours=1)


def quiet_third_party_logs() -> None:
    for name in THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


#: Telegram test environment data centres (https://core.telegram.org/api/auth#test-accounts).
#: The tool never connects anywhere else: there is no production fallback.
TEST_DC_ADDRESSES: dict[int, str] = {
    1: "149.154.175.10",
    2: "149.154.167.40",
    3: "149.154.175.117",
}
#: Every address a test-environment session may point at. Besides the documented
#: addresses above, the test servers' own ``help.getConfig`` advertises DC1 as
#: ``149.154.175.40``; Telethon follows that list on ``PHONE_MIGRATE`` (accounts
#: with e.g. +1 numbers live on test DC1). Production DCs are never listed here.
TEST_DC_ALLOWED_ADDRESSES: frozenset[str] = frozenset(
    {*TEST_DC_ADDRESSES.values(), "149.154.175.40"}
)
TEST_DC_PORT = 443
_INITIAL_TEST_DC = 2


def _session(session: Secret | None) -> StringSession:
    """A session pinned to the test environment; refuses sessions from anywhere else."""
    if session is None:
        fresh = StringSession()
        fresh.set_dc(_INITIAL_TEST_DC, TEST_DC_ADDRESSES[_INITIAL_TEST_DC], TEST_DC_PORT)
        return fresh
    try:
        restored = StringSession(session.reveal())
    except ValueError:
        raise SafetyError("会话格式无效，请重新执行 just e2e-login") from None
    if restored.server_address not in TEST_DC_ALLOWED_ADDRESSES:
        raise SafetyError("会话不属于 Telegram 测试环境，拒绝连接（不会回退到生产环境）")
    return restored


def _client(api: ApiCredentials, session: Secret | None) -> TelegramClient:
    return TelegramClient(
        _session(session),
        api.api_id,
        api.api_hash.reveal(),
        flood_sleep_threshold=0,
        receive_updates=False,
        device_model="akkiabot-e2e",
    )


async def ensure_test_environment(client: Any) -> None:
    """Ask the connected server itself; ``help.getConfig`` reports ``test_mode``."""
    config = await _guard(client(functions.help.GetConfigRequest()))
    address = getattr(client.session, "server_address", None)
    if not getattr(config, "test_mode", False) or address not in TEST_DC_ALLOWED_ADDRESSES:
        await _disconnect(client)
        raise SafetyError("连接的不是 Telegram 测试环境，已断开（不会回退到生产环境）")


async def _guard[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except errors.FloodError as exc:
        raise FloodWait(float(getattr(exc, "seconds", 0) or 60)) from None


async def _disconnect(client: TelegramClient) -> None:
    # Telethon's disconnect() returns a coroutine only when called inside a running loop.
    result: Any = client.disconnect()
    if inspect.isawaitable(result):
        await result


def _user(entity: Any) -> UserInfo:
    return UserInfo(
        id=int(entity.id),
        first_name=entity.first_name or "",
        username=entity.username,
        is_bot=bool(entity.bot),
    )


def _service_message_id(updates: Any) -> int | None:
    # Some methods (e.g. messages.importChatInvite on current layers) wrap the
    # ``Updates`` in another object whose ``.updates`` is itself an ``Updates``.
    inner = getattr(updates, "updates", None)
    while inner is not None and not isinstance(inner, list):
        inner = getattr(inner, "updates", None)
    for update in inner or ():
        message = getattr(update, "message", None)
        if isinstance(message, types.MessageService):
            return int(message.id)
    return None


def _buttons(message: Any) -> tuple[Button, ...]:
    markup = getattr(message, "reply_markup", None)
    if not isinstance(markup, types.ReplyInlineMarkup):
        return ()
    buttons: list[Button] = []
    for row in markup.rows:
        for button in row.buttons:
            # The current layer wraps the kind: KeyboardInlineButton(text, type=InlineButtonType*).
            kind = getattr(button, "type", None)
            buttons.append(
                Button(
                    text=button.text,
                    url=getattr(kind, "url", None),
                    data=getattr(kind, "data", None),
                    web_app=isinstance(kind, types.InlineButtonTypeWebView),
                )
            )
    return tuple(buttons)


def _msg(chat_id: int, message: Any) -> Msg:
    reply_to = getattr(message, "reply_to", None)
    entities = tuple(
        Entity(_ENTITY_KINDS.get(type(e), type(e).__name__), e.offset, e.length)
        for e in getattr(message, "entities", None) or ()
    )
    service = None
    if isinstance(message, types.MessageService):
        service = type(message.action).__name__
    return Msg(
        id=int(message.id),
        chat_id=chat_id,
        sender_id=getattr(message, "sender_id", None),
        text=getattr(message, "message", "") or "",
        reply_to_id=getattr(reply_to, "reply_to_msg_id", None),
        entities=entities,
        service=service,
        buttons=_buttons(message),
    )


class TelethonAccount:
    def __init__(self, client: TelegramClient) -> None:
        # Telethon's annotations are incomplete (it converts peers / ids on the fly).
        self._client: Any = client

    @classmethod
    async def connect(cls, api: ApiCredentials, session: Secret, role: str) -> TelethonAccount:
        client = _client(api, session)
        await client.connect()
        await ensure_test_environment(client)
        if not await client.is_user_authorized():
            await _disconnect(client)
            raise SafetyError(
                f"{role} 会话已失效，请重新执行 just e2e-login {role} 并更新 1Password"
            )
        account = cls(client)
        # Fill the entity cache so chats / users can be addressed by id.
        await _guard(client.get_dialogs(limit=200))
        return account

    async def _chat(self, chat_id: int) -> Any:
        try:
            return await self._client.get_input_entity(chat_id)
        except ValueError:
            await _guard(self._client.get_dialogs(limit=200))
            try:
                return await self._client.get_input_entity(chat_id)
            except ValueError:
                raise E2EError(f"当前账号看不到群 {chat_id}") from None

    async def _user_in(self, chat_id: int, user_id: int) -> Any:
        try:
            return await self._client.get_input_entity(user_id)
        except ValueError:
            await _guard(self._client.get_participants(await self._chat(chat_id), limit=200))
            return await self._client.get_input_entity(user_id)

    async def me(self) -> UserInfo:
        return _user(await _guard(self._client.get_me()))

    async def resolve_user(self, username: str) -> UserInfo:
        entity = await _guard(self._client.get_entity(username))
        if not isinstance(entity, types.User):
            raise E2EError(f"@{username} 不是用户或 bot")
        return _user(entity)

    async def find_chat(self, chat_id: int) -> ChatRef | None:
        try:
            entity = await _guard(self._client.get_entity(await self._chat(chat_id)))
        except (E2EError, ValueError, errors.ChannelPrivateError):
            return None
        if getattr(entity, "left", False):
            return None
        return ChatRef(utils.get_peer_id(entity), entity.title)

    async def owned_supergroups(self, title: str) -> list[ChatRef]:
        found: list[ChatRef] = []
        for dialog in await _guard(self._client.get_dialogs(limit=None)):
            entity = dialog.entity
            if (
                isinstance(entity, types.Channel)
                and entity.megagroup
                and entity.creator
                and entity.title == title
            ):
                found.append(ChatRef(utils.get_peer_id(entity), entity.title))
        return found

    async def owned_supergroups_with_prefix(self, prefix: str) -> list[ChatRef]:
        found: list[ChatRef] = []
        for dialog in await _guard(self._client.get_dialogs(limit=None)):
            entity = dialog.entity
            if (
                isinstance(entity, types.Channel)
                and entity.megagroup
                and entity.creator
                and entity.title.startswith(prefix)
            ):
                found.append(ChatRef(utils.get_peer_id(entity), entity.title))
        return found

    async def delete_supergroup(self, chat_id: int) -> None:
        await _guard(
            self._client(functions.channels.DeleteChannelRequest(await self._chat(chat_id)))
        )

    async def create_supergroup(self, title: str, about: str) -> ChatRef:
        result: Any = await _guard(
            self._client(
                functions.channels.CreateChannelRequest(title=title, about=about, megagroup=True)
            )
        )
        channel = result.chats[0]
        return ChatRef(utils.get_peer_id(channel), channel.title)

    async def chat_about(self, chat_id: int) -> str:
        result: Any = await _guard(
            self._client(functions.channels.GetFullChannelRequest(await self._chat(chat_id)))
        )
        return result.full_chat.about or ""

    async def _participant(self, chat_id: int, user_id: int) -> Any | None:
        try:
            result: Any = await _guard(
                self._client(
                    functions.channels.GetParticipantRequest(
                        channel=await self._chat(chat_id),
                        participant=await self._user_in(chat_id, user_id),
                    )
                )
            )
        except (errors.UserNotParticipantError, ValueError):
            return None
        return result.participant

    async def participant_role(self, chat_id: int, user_id: int) -> Role | None:
        participant = await self._participant(chat_id, user_id)
        if isinstance(participant, types.ChannelParticipantCreator):
            return "creator"
        if isinstance(participant, types.ChannelParticipantAdmin):
            return "admin"
        if isinstance(participant, types.ChannelParticipant | types.ChannelParticipantSelf):
            return "member"
        return None

    async def admin_rights(self, chat_id: int, user_id: int) -> frozenset[str]:
        participant = await self._participant(chat_id, user_id)
        rights = getattr(participant, "admin_rights", None)
        if rights is None:
            return frozenset()
        return frozenset(name for name, value in rights.to_dict().items() if value is True)

    async def invite(self, chat_id: int, user_id: int) -> None:
        user = await self._client.get_input_entity(user_id)
        await _guard(
            self._client(
                functions.channels.InviteToChannelRequest(await self._chat(chat_id), [user])
            )
        )

    async def promote(self, chat_id: int, user_id: int, rights: frozenset[str]) -> None:
        admin_rights = types.ChatAdminRights(**dict.fromkeys(rights, True))
        await _guard(
            self._client(
                functions.channels.EditAdminRequest(
                    channel=await self._chat(chat_id),
                    user_id=await self._user_in(chat_id, user_id),
                    admin_rights=admin_rights,
                    rank="",
                )
            )
        )

    async def export_invite_link(self, chat_id: int) -> str:
        result: Any = await _guard(
            self._client(
                functions.messages.ExportChatInviteRequest(
                    peer=await self._chat(chat_id),
                    expire_date=datetime.now(UTC) + _INVITE_TTL,
                    usage_limit=1,
                    title="akkiabot-e2e",
                )
            )
        )
        return str(result.link)

    async def join_by_link(self, link: str) -> int | None:
        invite_hash = link.rstrip("/").rsplit("/", 1)[-1].removeprefix("+")
        try:
            updates = await _guard(
                self._client(functions.messages.ImportChatInviteRequest(invite_hash))
            )
        except errors.UserAlreadyParticipantError:
            return None
        return _service_message_id(updates)

    async def leave(self, chat_id: int) -> int | None:
        updates = await _guard(
            self._client(functions.channels.LeaveChannelRequest(await self._chat(chat_id)))
        )
        return _service_message_id(updates)

    async def is_banned(self, chat_id: int, user_id: int) -> bool:
        participant = await self._participant(chat_id, user_id)
        if not isinstance(participant, types.ChannelParticipantBanned):
            return False
        return bool(participant.banned_rights.view_messages)

    async def is_restricted(self, chat_id: int, user_id: int) -> bool:
        participant = await self._participant(chat_id, user_id)
        if not isinstance(participant, types.ChannelParticipantBanned):
            return False
        rights = participant.banned_rights
        return bool(rights.send_messages) and not rights.view_messages

    async def unban(self, chat_id: int, user_id: int) -> None:
        # Empty rights remove the user from the banned list (Bot API ``unbanChatMember``).
        await _guard(
            self._client(
                functions.channels.EditBannedRequest(
                    channel=await self._chat(chat_id),
                    participant=await self._user_in(chat_id, user_id),
                    banned_rights=types.ChatBannedRights(until_date=None),
                )
            )
        )

    async def open_web_app(self, bot: UserInfo, url: str) -> str:
        # Same request a client sends when the user taps an inline web_app button
        # in the bot's private chat; Telegram signs initData for this account.
        peer = await _guard(self._client.get_input_entity(bot.username or bot.id))
        result: Any = await _guard(
            self._client(
                functions.messages.RequestWebViewRequest(
                    peer=peer, bot=peer, platform="tdesktop", url=url, from_bot_menu=False
                )
            )
        )
        launch = getattr(result, "url", None)
        if not isinstance(launch, str):
            raise E2EError("Telegram 没有返回 Mini App 启动地址")
        return launch

    async def click(self, chat_id: int, message_id: int, data: bytes) -> str | None:
        try:
            answer: Any = await _guard(
                self._client(
                    functions.messages.GetBotCallbackAnswerRequest(
                        peer=await self._chat(chat_id), msg_id=message_id, data=data
                    )
                )
            )
        except errors.BotResponseTimeoutError:
            return None
        return getattr(answer, "message", None)

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None) -> Msg:
        message = await _guard(
            self._client.send_message(
                await self._chat(chat_id),
                text,
                reply_to=reply_to,
                parse_mode=None,
                link_preview=False,
            )
        )
        return _msg(chat_id, message)

    async def history(self, chat_id: int, *, min_id: int, limit: int = 50) -> list[Msg]:
        messages: Any = await _guard(
            self._client.get_messages(await self._chat(chat_id), min_id=min_id, limit=limit)
        )
        return [_msg(chat_id, m) for m in reversed(list(messages)) if m is not None]

    async def get_message(self, chat_id: int, message_id: int) -> Msg | None:
        message: Any = await _guard(
            self._client.get_messages(await self._chat(chat_id), ids=message_id)
        )
        return None if message is None else _msg(chat_id, message)

    async def close(self) -> None:
        await _disconnect(self._client)


class TelethonLogin:
    """:class:`~akkiabot_e2e.login.LoginClient` on a fresh test-environment session.

    Third-party clients can no longer sign up new accounts, so the phone number
    must already be registered in the test environment (official app, test mode).
    """

    def __init__(self, api: ApiCredentials) -> None:
        self._client: Any = _client(api, None)

    async def login(self, phone: Ask, code: Ask, password: Ask) -> UserInfo:
        client = self._client
        await client.connect()
        await ensure_test_environment(client)
        number = phone().strip()
        sent = await _guard(client.send_code_request(number))
        for _ in range(3):
            try:
                await _guard(
                    client.sign_in(number, code().strip(), phone_code_hash=sent.phone_code_hash)
                )
                break
            except errors.SessionPasswordNeededError:
                await _guard(client.sign_in(password=password()))
                break
            except errors.PhoneNumberUnoccupiedError:
                raise E2EError(
                    "该手机号尚未在 Telegram 测试环境注册：请先用官方客户端的测试模式注册（见文档）"
                ) from None
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                print("验证码不正确，请重试。", flush=True)
        else:
            raise E2EError("验证码连续 3 次错误，已放弃")
        # A PHONE_MIGRATE may have moved us; it must still be a test DC.
        await ensure_test_environment(client)
        return _user(await _guard(client.get_me()))

    def session_string(self) -> str:
        session = self._client.session
        if not isinstance(session, StringSession):
            raise E2EError("unexpected Telethon session type")
        return session.save()

    async def close(self) -> None:
        await _disconnect(self._client)


async def check_test_environment(api: ApiCredentials) -> int:
    """Connect without logging in and confirm the server is the test environment."""
    client: Any = _client(api, None)
    await client.connect()
    try:
        await ensure_test_environment(client)
        return int(client.session.dc_id)
    finally:
        await _disconnect(client)


async def session_user(api: ApiCredentials, session: Secret) -> UserInfo:
    account = await TelethonAccount.connect(api, session, "session")
    try:
        return await account.me()
    finally:
        await account.close()


async def connect_account(api: ApiCredentials, role: str, session: Secret) -> TelethonAccount:
    return await TelethonAccount.connect(api, session, role)
