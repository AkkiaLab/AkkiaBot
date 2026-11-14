"""In-memory Telegram for unit tests: accounts, supergroups and a reference bot.

:class:`FakeBot` reproduces the user-visible M2/M3/M4 behaviour (rendered
MarkdownV2 = plain text + code entities). Knobs simulate regressions so tests
can prove that the scenarios catch them.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from akkiabot_e2e.errors import E2EError, FloodWait
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.transport import Account, Button, ChatRef, Entity, Msg, Role, UserInfo

ADMIN = UserInfo(1001, "群主测试", "e2e_admin")
MEMBER = UserInfo(1002, "成员测试", "e2e_member")
BOT = UserInfo(2001, "AkkiaBot Staging", "akkiabot_staging_bot", is_bot=True)
# mock: 人工构造的不可用 Telethon 会话串，不是凭据。
MOCK_ADMIN_SESSION = "1AdminSessionStringSECRET-aaaaaaaaaaaaaaaa"
MOCK_MEMBER_SESSION = "1MemberSessionStringSECRET-bbbbbbbbbbbbbbbb"  # mock: 同上

# mock: 人工构造的不可用 api_hash，不是凭据。
MOCK_API_HASH = "fake-api-hash-for-tests"
# mock: 人工构造的不可用 LLM key，不是凭据。
MOCK_LLM_API_KEY = "fake-llm-key-for-tests"


def live_env(**overrides: str) -> dict[str, str]:
    env = {
        "E2E_API_ID": "123456",
        "E2E_API_HASH": MOCK_API_HASH,
        "E2E_BOT_USERNAME": "@akkiabot_staging_bot",
        "E2E_ADMIN_SESSION": MOCK_ADMIN_SESSION,
        "E2E_MEMBER_SESSION": MOCK_MEMBER_SESSION,
        "E2E_ALLOWED_USER_IDS": f"{ADMIN.id},{MEMBER.id}",
        "E2E_ALLOWED_CHAT_IDS": "",
        "E2E_REPLY_TIMEOUT": "0.3",
        "E2E_SEND_INTERVAL": "0.001",
        "E2E_ANTISPAM_WAIT": "0.01",
        "LLM_API_KEY": MOCK_LLM_API_KEY,
        "LLM_MODEL": "fake-model",
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
    banned: set[int] = field(default_factory=set)
    restricted: set[int] = field(default_factory=set)
    """Muted members (cannot send messages)."""
    message_counts: dict[int, int] = field(default_factory=dict)
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


_SWITCHES = (
    ("antispam", "反垃圾"),
    ("captcha", "入群验证"),
    ("warn", "警告"),
    ("delete_service_messages", "删除系统消息"),
)


def _mention(user: UserInfo, rest: str) -> tuple[str, tuple[Entity, ...]]:
    length = len(user.first_name.encode("utf-16-le")) // 2
    return user.first_name + rest, (Entity("mention_name", 0, length),)


@dataclass
class FakeBot:
    user: UserInfo
    silent: bool = False
    leave_escapes: bool = False
    delete_service: bool = True
    ip_unknown_isp: bool = False
    # -- M4 knobs (regressions the scenarios must catch) --
    warn_limit: int = 3
    no_purge: bool = False
    """Ban without deleting the member's history."""
    no_ban: bool = False
    """Reply as if banned, but never ban."""
    members_can_moderate: bool = False
    """Skip the permission check."""
    stale_menu: bool = False
    """Apply a toggle but do not redraw the menu."""
    # -- M5 knobs --
    antispam_ignores_switch: bool = False
    """Remove spam even when the switch is off."""
    antispam_no_exemption: bool = False
    """Check long-standing members too."""
    antispam_no_mute: bool = False
    """Delete spam but never mute."""
    # -- state --
    settings: dict[int, dict[str, bool]] = field(default_factory=dict, init=False)
    warns: dict[tuple[int, int], int] = field(default_factory=dict, init=False)
    tokens: dict[str, tuple[int, int]] = field(default_factory=dict, init=False)
    _token_ids: itertools.count[int] = field(default_factory=itertools.count, init=False)

    def _switches(self, chat_id: int) -> dict[str, bool]:
        defaults = {name: name == "delete_service_messages" for name, _ in _SWITCHES}
        return self.settings.setdefault(chat_id, defaults)

    def can_moderate(self, chat: FakeChat, user_id: int) -> bool:
        role = chat.members.get(user_id)
        return (
            self.members_can_moderate
            or role == "creator"
            or (role == "admin" and "ban_users" in chat.rights.get(user_id, frozenset()))
        )

    def menu(self, chat: FakeChat) -> tuple[str, tuple[Button, ...]]:
        switches = self._switches(chat.id)
        buttons = tuple(
            Button(
                f"{'✅' if switches[name] else '❌'} {label}",
                data=f"cfg:{chat.id}:{name}:{int(not switches[name])}".encode(),
            )
            for name, label in _SWITCHES
        )
        return f"群名：{chat.title}\n功能开关（点击切换）", buttons

    def respond_private(self, world: FakeTelegram, private: FakeChat, msg: Msg) -> None:
        command, _, token = msg.text.partition(" ")
        if command != "/start":
            return
        bound = self.tokens.pop(token, None)
        if bound is None:
            world.post(private, self.user.id, "链接已失效或已被使用，请回到群里重新获取。")
            return
        chat_id, user_id = bound
        if user_id != msg.sender_id:
            world.post(private, self.user.id, "验证失败：您不是发起配置的用户。")
            return
        text, buttons = self.menu(world.chats[chat_id])
        # Like PTB's reply_text(): no quote in private chats.
        world.post(private, self.user.id, text, buttons=buttons)

    def on_callback(
        self, world: FakeTelegram, presser: int, private: FakeChat, message_id: int, data: bytes
    ) -> str:
        _, chat_id, name, value = data.decode().split(":")
        chat = world.chats[int(chat_id)]
        if not self.can_moderate(chat, presser):
            return "需要群主或具有封禁成员权限的管理员才能修改设置"
        self._switches(chat.id)[name] = value == "1"
        if not self.stale_menu:
            text, buttons = self.menu(chat)
            old = private.messages[message_id]
            private.messages[message_id] = Msg(
                old.id, old.chat_id, old.sender_id, text, old.reply_to_id, buttons=buttons
            )
        return "设置成功"

    def _moderate(
        self, world: FakeTelegram, chat: FakeChat, msg: Msg, reply: Msg | None, verb: str
    ) -> UserInfo | str:
        if reply is None or reply.sender_id is None:
            return f"此命令需要以回复的方式使用，将{verb}被回复的成员。"
        if not self.can_moderate(chat, msg.sender_id or 0):
            return f"你没有权限{verb}成员：仅群主或具有封禁成员权限的管理员可用。"
        if chat.members.get(reply.sender_id) in ("creator", "admin"):
            return f"无法{verb}管理员或群主。"
        return world.users[reply.sender_id]

    def _ban(self, chat: FakeChat, target: UserInfo) -> int:
        if self.no_ban:
            return 0
        chat.members.pop(target.id, None)
        chat.banned.add(target.id)
        if self.no_purge:
            return 0
        doomed = [i for i, m in chat.messages.items() if m.sender_id == target.id and not m.service]
        for message_id in doomed:
            del chat.messages[message_id]
        return len(doomed)

    def respond_m4(
        self, world: FakeTelegram, chat: FakeChat, msg: Msg, command: str, reply: Msg | None
    ) -> tuple[str, tuple[Entity, ...], tuple[Button, ...]] | None:
        match command:
            case "/config":
                if not self.can_moderate(chat, msg.sender_id or 0):
                    return "需要群主或具有封禁成员权限的管理员才能使用 /config。", (), ()
                token = f"tok{next(self._token_ids)}"
                self.tokens[token] = (chat.id, msg.sender_id or 0)
                link = f"https://t.me/{self.user.username}?start={token}"
                return "功能设置", (), (Button("👉点击按钮开始设置👈", url=link),)
            case "/ban":
                target = self._moderate(world, chat, msg, reply, "封禁")
                if isinstance(target, str):
                    return target, (), ()
                deleted = self._ban(chat, target)
                text, entities = _mention(target, f" 已被封禁，删除了 {deleted} 条消息。")
                return text, entities, ()
            case "/warn":
                if not self._switches(chat.id)["warn"]:
                    return "请先通过 /config 开启警告功能。", (), ()
                target = self._moderate(world, chat, msg, reply, "警告")
                if isinstance(target, str):
                    return target, (), ()
                key = (chat.id, target.id)
                count = self.warns.get(key, 0) + 1
                self.warns[key] = count
                rest = f" 你已被警告 {count}/{self.warn_limit} 次"
                if count >= self.warn_limit:
                    self.warns[key] = 0
                    deleted = self._ban(chat, target)
                    rest += f"，警告次数已达上限，已被封禁。\n删除了 {deleted} 条消息。"
                text, entities = _mention(target, rest)
                return text, entities, ()
        return None

    def check_spam(self, world: FakeTelegram, chat: FakeChat, msg: Msg) -> None:
        """Fake LLM: "彩金" means spam. Mirrors the guard's switch / exemption rules."""
        sender = msg.sender_id or 0
        chat.message_counts[sender] = count = chat.message_counts.get(sender, 0) + 1
        enabled = self._switches(chat.id)["antispam"] or self.antispam_ignores_switch
        if not enabled or chat.members.get(sender) in ("creator", "admin"):
            return
        if count > 3 and not self.antispam_no_exemption:
            return
        if "彩金" not in msg.text:
            return
        chat.messages.pop(msg.id, None)
        if not self.antispam_no_mute:
            chat.restricted.add(sender)
        text, entities = _mention(world.users[sender], "")
        notice = f"[Anti SPAM]\n内容命中反垃圾策略\n用户：{text}\n动作：删除消息、禁言"
        offset = len("[Anti SPAM]\n内容命中反垃圾策略\n用户：".encode("utf-16-le")) // 2
        world.post(
            chat,
            self.user.id,
            notice,
            entities=tuple(Entity(e.kind, e.offset + offset, e.length) for e in entities),
        )

    def respond(self, world: FakeTelegram, chat: FakeChat, msg: Msg) -> None:
        if chat.id == self.user.id:
            self.respond_private(world, chat, msg)
            return
        if self.user.id not in chat.members:
            return
        if msg.service is not None:
            if self.delete_service and "delete_messages" in chat.rights.get(
                self.user.id, frozenset()
            ):
                chat.messages.pop(msg.id, None)
            return
        if not msg.text.startswith("/"):
            self.check_spam(world, chat, msg)
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
            case "/spam":
                entities = ()
                if reply is None or reply.sender_id is None:
                    text = "此命令需要以回复的方式使用，将举报被回复的成员。"
                elif chat.members.get(reply.sender_id) in ("creator", "admin"):
                    text = "无法举报管理员或群主。"
                elif self.can_moderate(chat, sender.id):
                    target = world.users[reply.sender_id]
                    deleted = self._ban(chat, target)
                    text, entities = _mention(
                        target, f" 已被封禁（垃圾信息），删除了 {deleted} 条消息。"
                    )
                else:
                    text = "举报已收到，正在进行机器审核……"
                world.post(chat, self.user.id, text, reply_to=msg.id, entities=entities)
                return
            case "/config" | "/ban" | "/warn":
                answer = self.respond_m4(world, chat, msg, command, reply)
                if answer is None:
                    return
                text, entities, buttons = answer
                world.post(
                    chat, self.user.id, text, reply_to=msg.id, entities=entities, buttons=buttons
                )
                return
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
        buttons: tuple[Button, ...] = (),
    ) -> Msg:
        msg = Msg(chat.next_id, chat.id, sender_id, text, reply_to, entities, service, buttons)
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
        bot = self.world.bot
        is_bot_chat = bot is not None and chat_id == bot.user.id
        if chat_id not in self.world.chats and (chat_id in self.world.private_bots or is_bot_chat):
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
        if self.user.id in chat.banned:
            raise E2EError("USER_BANNED_IN_CHANNEL")
        chat.members[self.user.id] = "member"
        msg = self.world.post(chat, self.user.id, "", service="chat_joined_by_link")
        return msg.id if self.world.service_ids["join"] else None

    async def leave(self, chat_id: int) -> int | None:
        self._op("leave")
        chat = self._chat(chat_id)
        chat.members.pop(self.user.id, None)
        msg = self.world.post(chat, self.user.id, "", service="chat_delete_user")
        return msg.id if self.world.service_ids["leave"] else None

    async def is_banned(self, chat_id: int, user_id: int) -> bool:
        self._op("is_banned")
        return user_id in self._chat(chat_id).banned

    async def is_restricted(self, chat_id: int, user_id: int) -> bool:
        self._op("is_restricted")
        return user_id in self._chat(chat_id).restricted

    async def unban(self, chat_id: int, user_id: int) -> None:
        self._op("unban")
        self._chat(chat_id).banned.discard(user_id)
        self._chat(chat_id).restricted.discard(user_id)

    async def click(self, chat_id: int, message_id: int, data: bytes) -> str | None:
        self._op("click")
        bot = self.world.bot
        assert bot is not None  # noqa: S101
        assert chat_id == bot.user.id  # noqa: S101 - only the bot sends buttons here
        return bot.on_callback(self.world, self.user.id, self._chat(chat_id), message_id, data)

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


class FakeTraces:
    """:class:`~akkiabot_e2e.scenarios.TraceProbe` answering from a fixed set of traced markers.

    ``traced=None`` means "everything asked for was traced".
    """

    def __init__(self, traced: set[str] | None = None) -> None:
        self.traced = traced
        self.asked: list[str] = []

    async def wait_for_markers(self, markers: Sequence[str], *, since: datetime) -> dict[str, int]:
        self.asked += markers
        return {m: int(self.traced is None or m in self.traced) for m in markers}


class Connector:
    """``Connect`` for the runner: maps session secrets to fake accounts."""

    def __init__(self, world: FakeTelegram, sessions: dict[str, UserInfo] | None = None) -> None:
        self.world = world
        self.sessions = sessions or {MOCK_ADMIN_SESSION: ADMIN, MOCK_MEMBER_SESSION: MEMBER}
        self.accounts: list[FakeAccount] = []

    async def __call__(self, role: str, session: Secret) -> Account:
        account = self.world.account(self.sessions[session.reveal()])
        self.accounts.append(account)
        return account


BOTFATHER = UserInfo(93372553, "BotFather", "BotFather", is_bot=True)
# mock: 人工构造的不可用 bot token，不符合真实格式，不是凭据。
MOCK_BOT_TOKEN = "123456:fake-bot-token-for-tests-xxxxxxxxxxx"  # noqa: S105


def add_fake_botfather(world: FakeTelegram, *, taken: int = 0, token: str = MOCK_BOT_TOKEN) -> None:
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
