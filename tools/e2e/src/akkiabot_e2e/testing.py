"""In-memory Telegram for unit tests: accounts, supergroups and a reference bot.

:class:`FakeBot` reproduces the user-visible M2/M3 behaviour (rendered
MarkdownV2 = plain text + code entities). Knobs simulate regressions so tests
can prove that the scenarios catch them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from akkiabot_e2e.errors import FloodWait
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.transport import Account, ChatRef, Entity, Msg, Role, UserInfo

ADMIN = UserInfo(1001, "群主测试", "e2e_admin")
MEMBER = UserInfo(1002, "成员测试", "e2e_member")
BOT = UserInfo(2001, "AkkiaBot Staging", "akkiabot_staging_bot", is_bot=True)
ADMIN_SESSION = "1AdminSessionStringSECRET-aaaaaaaaaaaaaaaa"
MEMBER_SESSION = "1MemberSessionStringSECRET-bbbbbbbbbbbbbbbb"


def live_env(**overrides: str) -> dict[str, str]:
    env = {
        "E2E_API_ID": "123456",
        "E2E_API_HASH": "fake-api-hash-for-tests",
        "E2E_BOT_USERNAME": "@akkiabot_staging_bot",
        "E2E_ADMIN_SESSION": ADMIN_SESSION,
        "E2E_MEMBER_SESSION": MEMBER_SESSION,
        "E2E_ALLOWED_USER_IDS": f"{ADMIN.id},{MEMBER.id}",
        "E2E_ALLOWED_CHAT_IDS": "",
        "E2E_REPLY_TIMEOUT": "0.3",
        "E2E_SEND_INTERVAL": "0.001",
    }
    env.update(overrides)
    return env


@dataclass
class FakeChat:
    id: int
    title: str
    creator_id: int
    members: dict[int, Role] = field(default_factory=dict)
    rights: dict[int, frozenset[str]] = field(default_factory=dict)
    messages: dict[int, Msg] = field(default_factory=dict)
    next_id: int = 1


def rendered(*parts: str | tuple[str]) -> tuple[str, tuple[Entity, ...]]:
    """Build text; ``(value,)`` parts become code entities (offsets in UTF-16 units)."""
    text, entities = "", []
    for part in parts:
        if isinstance(part, tuple):
            value = part[0]
            offset = len(text.encode("utf-16-le")) // 2
            entities.append(Entity("code", offset, len(value.encode("utf-16-le")) // 2))
            text += value
        else:
            text += part
    return text, tuple(entities)


@dataclass
class FakeBot:
    user: UserInfo
    silent: bool = False
    leave_escapes: bool = False
    delete_service: bool = True
    ip_unknown_isp: bool = False

    def respond(self, world: FakeTelegram, chat: FakeChat, msg: Msg) -> None:
        if self.user.id not in chat.members:
            return
        if msg.service is not None:
            if self.delete_service and "delete_messages" in chat.rights.get(
                self.user.id, frozenset()
            ):
                chat.messages.pop(msg.id, None)
            return
        if self.silent or not msg.text.startswith("/"):
            return
        command, _, arg = msg.text.partition(" ")
        reply = chat.messages.get(msg.reply_to_id) if msg.reply_to_id else None
        sender = world.users[msg.sender_id or 0]
        match command:
            case "/help":
                text, entities = rendered(
                    "可用命令列表：\n1. 获取帮助信息\n用法：/help [命令名]\n2. 获取ID信息\n用法：/id"
                )
            case "/id":
                parts: list[str | tuple[str]] = [
                    f"当前ChatId: {chat.title} (",
                    (str(chat.id),),
                    ")\n",
                    f"你的UserId: {sender.first_name} (",
                    (str(sender.id),),
                    ")",
                ]
                if reply is not None and reply.sender_id is not None:
                    target = world.users[reply.sender_id]
                    parts += [f"\n回复目标的UserId: {target.first_name} (", (str(target.id),), ")"]
                text, entities = rendered(*parts)
            case "/ip":
                ip = arg.strip() or ("1.1.1.1" if reply and "1.1.1.1" in reply.text else "")
                isp = "未知" if self.ip_unknown_isp else "Cloudflare, Inc."
                text, entities = rendered(
                    "IP: ",
                    (ip,),
                    "\nLocation: ",
                    ("Brisbane, AU",),
                    "\nAS: ",
                    ("AS13335",),
                    "\nOrg: ",
                    ("APNIC and Cloudflare DNS Resolver project",),
                    "\nISP: ",
                    (isp,),
                )
            case _:
                return
        if self.leave_escapes:
            text = text.replace("(", "\\(").replace(")", "\\)")
        world.post(chat, self.user.id, text, reply_to=msg.id, entities=entities)


class FakeTelegram:
    def __init__(self) -> None:
        self.users: dict[int, UserInfo] = {}
        self.chats: dict[int, FakeChat] = {}
        self.bot: FakeBot | None = None
        self.service_ids: dict[str, bool] = {"join": True, "leave": True}
        """Whether join / leave report their service message id (Telegram may omit it)."""
        self.floods: dict[str, list[float]] = defaultdict(list)
        """Pending flood waits per operation name, raised once each."""
        self.calls: list[str] = []
        self.private_bots: dict[int, Callable[[str], str]] = {}
        self._next_chat = 1_000_000

    def add_user(self, user: UserInfo) -> UserInfo:
        self.users[user.id] = user
        return user

    def add_bot(self, user: UserInfo, **knobs: bool) -> FakeBot:
        self.add_user(user)
        self.bot = FakeBot(user, **knobs)
        return self.bot

    def add_private_bot(self, user: UserInfo, reply: Callable[[str], str]) -> None:
        """A bot answering private messages (e.g. BotFather); its chat id is its user id."""
        self.add_user(user)
        self.private_bots[user.id] = reply

    def add_chat(self, title: str, creator: UserInfo) -> FakeChat:
        self._next_chat += 1
        chat = FakeChat(-1_000_000_000_000 - self._next_chat, title, creator.id)
        chat.members[creator.id] = "creator"
        self.chats[chat.id] = chat
        return chat

    def post(
        self,
        chat: FakeChat,
        sender_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        entities: tuple[Entity, ...] = (),
        service: str | None = None,
    ) -> Msg:
        msg = Msg(chat.next_id, chat.id, sender_id, text, reply_to, entities, service)
        chat.next_id += 1
        chat.messages[msg.id] = msg
        if chat.id in self.private_bots and sender_id != chat.id:
            self.post(chat, chat.id, self.private_bots[chat.id](text))
        elif self.bot is not None and sender_id != self.bot.user.id:
            self.bot.respond(self, chat, msg)
        return msg

    def account(self, user: UserInfo) -> FakeAccount:
        return FakeAccount(self, user)


class FakeAccount:
    def __init__(self, world: FakeTelegram, user: UserInfo) -> None:
        self.world = world
        self.user = user
        self.closed = False

    def _op(self, name: str) -> None:
        self.world.calls.append(name)
        pending = self.world.floods.get(name)
        if pending:
            raise FloodWait(pending.pop(0))

    def _chat(self, chat_id: int) -> FakeChat:
        if chat_id not in self.world.chats and chat_id in self.world.private_bots:
            peer = self.world.users[chat_id]
            chat = FakeChat(chat_id, peer.first_name, self.user.id)
            chat.members.update({self.user.id: "member", chat_id: "member"})
            self.world.chats[chat_id] = chat
        return self.world.chats[chat_id]

    async def me(self) -> UserInfo:
        self._op("me")
        return self.user

    async def resolve_user(self, username: str) -> UserInfo:
        self._op("resolve_user")
        for user in self.world.users.values():
            if user.username == username:
                return user
        raise ValueError(username)

    async def find_chat(self, chat_id: int) -> ChatRef | None:
        self._op("find_chat")
        chat = self.world.chats.get(chat_id)
        if chat is None or self.user.id not in chat.members:
            return None
        return ChatRef(chat.id, chat.title)

    async def owned_supergroups(self, title: str) -> list[ChatRef]:
        self._op("owned_supergroups")
        return [
            ChatRef(c.id, c.title)
            for c in self.world.chats.values()
            if c.creator_id == self.user.id and c.title == title
        ]

    async def create_supergroup(self, title: str, about: str) -> ChatRef:
        self._op("create_supergroup")
        chat = self.world.add_chat(title, self.user)
        return ChatRef(chat.id, chat.title)

    async def participant_role(self, chat_id: int, user_id: int) -> Role | None:
        self._op("participant_role")
        return self._chat(chat_id).members.get(user_id)

    async def admin_rights(self, chat_id: int, user_id: int) -> frozenset[str]:
        self._op("admin_rights")
        return self._chat(chat_id).rights.get(user_id, frozenset())

    async def invite(self, chat_id: int, user_id: int) -> None:
        self._op("invite")
        self._chat(chat_id).members.setdefault(user_id, "member")

    async def promote(self, chat_id: int, user_id: int, rights: frozenset[str]) -> None:
        self._op("promote")
        chat = self._chat(chat_id)
        chat.members[user_id] = "admin"
        chat.rights[user_id] = rights

    async def export_invite_link(self, chat_id: int) -> str:
        self._op("export_invite_link")
        return f"https://t.me/+fake{-chat_id}"

    async def join_by_link(self, link: str) -> int | None:
        self._op("join_by_link")
        chat = self._chat(-int(link.rsplit("fake", 1)[1]))
        chat.members[self.user.id] = "member"
        msg = self.world.post(chat, self.user.id, "", service="chat_joined_by_link")
        return msg.id if self.world.service_ids["join"] else None

    async def leave(self, chat_id: int) -> int | None:
        self._op("leave")
        chat = self._chat(chat_id)
        chat.members.pop(self.user.id, None)
        msg = self.world.post(chat, self.user.id, "", service="chat_delete_user")
        return msg.id if self.world.service_ids["leave"] else None

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None) -> Msg:
        self._op("send")
        return self.world.post(self._chat(chat_id), self.user.id, text, reply_to=reply_to)

    async def history(self, chat_id: int, *, min_id: int, limit: int = 50) -> list[Msg]:
        self._op("history")
        msgs = sorted(m for m in self._chat(chat_id).messages if m > min_id)
        return [self._chat(chat_id).messages[i] for i in msgs[:limit]]

    async def get_message(self, chat_id: int, message_id: int) -> Msg | None:
        self._op("get_message")
        return self._chat(chat_id).messages.get(message_id)

    async def close(self) -> None:
        self.closed = True


class Connector:
    """``Connect`` for the runner: maps session secrets to fake accounts."""

    def __init__(self, world: FakeTelegram, sessions: dict[str, UserInfo] | None = None) -> None:
        self.world = world
        self.sessions = sessions or {ADMIN_SESSION: ADMIN, MEMBER_SESSION: MEMBER}
        self.accounts: list[FakeAccount] = []

    async def __call__(self, role: str, session: Secret) -> Account:
        account = self.world.account(self.sessions[session.reveal()])
        self.accounts.append(account)
        return account


BOTFATHER = UserInfo(93372553, "BotFather", "BotFather", is_bot=True)
FAKE_BOT_TOKEN = "123456:fake-bot-token-for-tests-xxxxxxxxxxx"  # noqa: S105 - synthetic


def add_fake_botfather(world: FakeTelegram, *, taken: int = 0, token: str = FAKE_BOT_TOKEN) -> None:
    """@BotFather that rejects the first ``taken`` usernames, then returns ``token``."""
    state = {"taken": taken}

    def reply(text: str) -> str:
        if text == "/newbot":
            return "Alright, a new bot. How are we going to call it? Please choose a name for your bot."
        if not text.endswith("_bot"):
            return "Good. Now let's choose a username for your bot."
        if state["taken"]:
            state["taken"] -= 1
            return "Sorry, this username is already taken. Please try something different."
        return f"Done! Use this token to access the HTTP API:\n{token}\nKeep your token secure."

    world.add_private_bot(BOTFATHER, reply)
