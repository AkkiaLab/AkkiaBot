"""Transport-neutral view of a logged-in Telegram *user* account.

Scenarios and group setup only talk to :class:`Account`; the Telethon adapter
(:mod:`akkiabot_e2e.telethon_transport`) and the in-memory fake
(:mod:`akkiabot_e2e.testing`) implement it. Chat ids use the Bot API form
(``-100…`` for supergroups) so reports match what the bot itself prints.
Transports raise :class:`~akkiabot_e2e.errors.FloodWait` for rate limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Role = Literal["creator", "admin", "member"]

#: Admin rights the bot needs in the test group. ``delete_messages`` is used by
#: M2 (service messages); ``ban_users`` by M4 (/ban, /warn) and M5 (/spam).
BOT_ADMIN_RIGHTS: frozenset[str] = frozenset({"delete_messages", "ban_users"})


@dataclass(frozen=True, slots=True)
class UserInfo:
    id: int
    first_name: str
    username: str | None = None
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class ChatRef:
    id: int
    title: str


@dataclass(frozen=True, slots=True)
class Entity:
    """A formatting entity; ``offset``/``length`` are UTF-16 code units (as Telegram sends)."""

    kind: str
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class Button:
    """An inline keyboard button: a URL, a callback or a Mini App (``web_app``) button."""

    text: str
    url: str | None = None
    data: bytes | None = None
    web_app: bool = False
    """``url`` opens a Mini App (Bot API ``web_app`` button) instead of a link."""


@dataclass(frozen=True, slots=True)
class Msg:
    id: int
    chat_id: int
    sender_id: int | None
    text: str
    reply_to_id: int | None = None
    entities: tuple[Entity, ...] = field(default_factory=tuple)
    service: str | None = None
    """Service action name (e.g. ``"chat_joined_by_link"``); ``None`` for normal messages."""
    buttons: tuple[Button, ...] = ()
    """Inline keyboard buttons, row by row."""

    def button(self, label: str) -> Button | None:
        """The first button whose text contains ``label``."""
        return next((b for b in self.buttons if label in b.text), None)


def entity_text(text: str, entity: Entity) -> str:
    """Substring covered by ``entity`` (offsets are UTF-16 code units)."""
    encoded = text.encode("utf-16-le")
    start, end = entity.offset * 2, (entity.offset + entity.length) * 2
    return encoded[start:end].decode("utf-16-le")


def code_spans(msg: Msg) -> list[str]:
    return [entity_text(msg.text, e) for e in msg.entities if e.kind in {"code", "pre"}]


class Account(Protocol):
    """One logged-in user account. All methods may raise ``FloodWait``."""

    async def me(self) -> UserInfo: ...

    async def resolve_user(self, username: str) -> UserInfo: ...

    async def find_chat(self, chat_id: int) -> ChatRef | None:
        """The chat if this account is currently a participant."""
        ...

    async def owned_supergroups(self, title: str) -> list[ChatRef]:
        """Supergroups created by this account whose title is exactly ``title``."""
        ...

    async def create_supergroup(self, title: str, about: str) -> ChatRef: ...

    async def chat_about(self, chat_id: int) -> str:
        """The group's description ("about"); ``""`` when it has none."""
        ...

    async def participant_role(self, chat_id: int, user_id: int) -> Role | None: ...

    async def admin_rights(self, chat_id: int, user_id: int) -> frozenset[str]: ...

    async def invite(self, chat_id: int, user_id: int) -> None: ...

    async def promote(self, chat_id: int, user_id: int, rights: frozenset[str]) -> None: ...

    async def ensure_contact(self, user: UserInfo) -> bool:
        """Make ``user`` one of this account's contacts; ``True`` if it was added now.

        The owner can only add the member to a group when both are contacts of
        each other (default invite privacy; ``USER_NOT_MUTUAL_CONTACT``).
        """
        ...

    async def export_invite_link(self, chat_id: int) -> str: ...

    async def join_by_link(self, link: str) -> int | None:
        """Join; returns the id of the resulting service message, if Telegram reports one."""
        ...

    async def leave(self, chat_id: int) -> int | None:
        """Leave; returns the id of the resulting service message, if any."""
        ...

    async def is_banned(self, chat_id: int, user_id: int) -> bool:
        """Whether ``user_id`` is in the chat's banned (kicked) list."""
        ...

    async def is_restricted(self, chat_id: int, user_id: int) -> bool:
        """Whether ``user_id`` is a restricted member who may not send messages (muted)."""
        ...

    async def unban(self, chat_id: int, user_id: int) -> None:
        """Lift a ban or a restriction (a banned user can join again; they are not re-added)."""
        ...

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None) -> Msg:
        """Send ``text``; for a private chat with a user or bot, ``chat_id`` is their user id."""
        ...

    async def click(self, chat_id: int, message_id: int, data: bytes) -> str | None:
        """Press a callback button; returns the bot's answer text (``None`` if none)."""
        ...

    async def open_web_app(self, bot: UserInfo, url: str) -> str:
        """Open the Mini App behind a ``web_app`` button sent by ``bot`` in its private chat.

        Returns the launch URL Telegram hands to the client: ``url`` plus a
        ``#tgWebAppData=…`` fragment with the initData signed for this account.
        """
        ...

    async def history(self, chat_id: int, *, min_id: int, limit: int = 50) -> list[Msg]:
        """Messages with ``id > min_id``, oldest first."""
        ...

    async def get_message(self, chat_id: int, message_id: int) -> Msg | None:
        """``None`` when the message does not exist (any more)."""
        ...

    async def close(self) -> None: ...
