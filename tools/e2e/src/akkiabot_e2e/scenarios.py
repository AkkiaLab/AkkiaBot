"""Acceptance scenarios, driven by real user accounts.

Each scenario sends messages as a test user, waits for the bot's reply and
checks what a user actually sees: the rendered text (MarkdownV2 must be
consumed, i.e. no escape backslashes) and the code entities around values.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from akkiabot_e2e.errors import CheckFailed, ScenarioSkipped, SideEffectsUnsettled
from akkiabot_e2e.group import owner_add_member
from akkiabot_e2e.transport import Account, ChatRef, Msg, UserInfo, code_spans

Status = Literal["passed", "failed", "error", "skipped"]


class TraceProbe(Protocol):
    """Looks up the bot's LLM traces (Langfuse) by run-specific markers."""

    async def wait_for_markers(
        self, markers: Sequence[str], *, since: datetime
    ) -> dict[str, int]: ...


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)
    """Lower-cased header names."""

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


class WebProbe(Protocol):
    """Plain HTTP against the deployed Mini App and API (captcha)."""

    async def get(self, url: str) -> HttpResult: ...

    async def post_json(self, url: str, body: Mapping[str, Any], *, origin: str) -> HttpResult: ...


@dataclass(frozen=True, slots=True)
class MiniAppVisit:
    """What a headless browser saw after opening the Mini App (local test-key runs)."""

    phase: Literal["passed", "failed", "timeout"]
    """``passed``: the page reports success; ``failed``: it shows an error
    (``role="alert"``); ``timeout``: neither appeared in time."""
    text: str
    """Visible status / error text of the page."""
    verify_calls: tuple[tuple[int, str | None], ...] = ()
    """``POST /api/v1/captcha/verify`` responses seen by the page: (HTTP status, ``error``)."""
    client_events: tuple[str, ...] = ()
    """Events the page posted to the (emulated) Telegram client, e.g. ``web_app_close``."""


class TurnstileLab(Protocol):
    """Local Mini App builds with Cloudflare's official Turnstile test keys (``--serve`` only).

    ``pass_origin`` is the build with the always-passing site key (the bot's
    ``WEBAPP_URL``), ``widget_fail_origin`` the same app built with the
    always-failing site key. The bot under test starts with the always-failing
    secret key; :meth:`use_secret` restarts it with the other one.
    ``siteverify`` names the secret key in use (``pass`` / ``fail``).
    """

    pass_origin: str
    widget_fail_origin: str
    siteverify: Literal["pass", "fail"]

    def describe(self) -> list[str]:
        """Report lines naming the test keys in use (all public Cloudflare test values)."""
        ...

    async def use_secret(self, which: Literal["pass", "fail"]) -> None: ...

    async def visit(self, url: str) -> MiniAppVisit: ...


@dataclass(slots=True)
class ScenarioContext:
    admin: Account
    member: Account
    admin_user: UserInfo
    member_user: UserInfo
    bot: UserInfo
    chat: ChatRef
    run_id: str
    reply_timeout: float = 30.0
    poll_interval: float = 1.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    plain_message_ids: list[int] = field(default_factory=list)
    """Ordinary (non-command) group messages sent this run; used for the message_log check."""
    antispam: bool = False
    """The bot under test has an LLM configured (antispam)."""
    antispam_wait: float = 20.0
    antispam_timeout: float = 60.0
    """How long the bot may take to act on spam (its classification budget + margin)."""
    antispam_purge_delay: float = 5.0
    """The bot's delay between muting a spammer and purging their history."""
    traces: TraceProbe | None = None
    classified_markers: list[str] = field(default_factory=list)
    """Markers of messages the bot must have classified (checked in Langfuse)."""
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    web: WebProbe | None = None
    api_base_url: str | None = None
    """Public API of the bot under test (captcha)."""
    webapp_url: str | None = None
    """Expected Mini App origin (captcha); ``None`` accepts whatever the bot links to."""
    not_automated: list[str] = field(default_factory=list)
    """Steps a scenario could not perform automatically; reported, never counted as passed."""
    turnstile_lab: TurnstileLab | None = None
    """Local Mini App with Turnstile test keys (``captcha.test-keys``, ``--serve`` only)."""
    test_keys: list[str] = field(default_factory=list)
    """Scenarios that ran on Cloudflare's test keys: reported as their own class of evidence."""

    def record(self, step: str, msg: Msg | None = None, **extra: Any) -> None:
        item: dict[str, Any] = {"step": step}
        if msg is not None:
            item.update(message_id=msg.id, sender_id=msg.sender_id, text=msg.text)
        item.update(extra)
        self.evidence.append(item)


ScenarioFn = Callable[[ScenarioContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    """``<group>.<case>``, e.g. ``moderation.ban``."""
    description: str
    run: ScenarioFn

    @property
    def group(self) -> str:
        """The feature the scenario covers (``antispam``, ``moderation`` ...); selectable."""
        return self.name.split(".", 1)[0]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


async def wait_for_reply(ctx: ScenarioContext, account: Account, request: Msg) -> Msg:
    """Poll until the bot replies to ``request`` (in ``request``'s chat); fail after
    ``ctx.reply_timeout``.

    In groups the reply must quote ``request``. In the private chat with the bot
    (positive chat id) the bot does not quote, so its next message is the reply.
    """
    private = request.chat_id > 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while True:
        for msg in await account.history(request.chat_id, min_id=request.id):
            if msg.sender_id == ctx.bot.id and (private or msg.reply_to_id == request.id):
                return msg
        if loop.time() >= deadline:
            raise CheckFailed(f"{ctx.reply_timeout:g} 秒内没有收到 bot 对消息 {request.id} 的回复")
        await asyncio.sleep(ctx.poll_interval)


async def ask(ctx: ScenarioContext, text: str, *, reply_to: int | None = None) -> Msg:
    """The member sends ``text`` (optionally as a reply) and returns the bot's reply."""
    request = await ctx.member.send(ctx.chat.id, text, reply_to=reply_to)
    ctx.record("成员发送", request)
    reply = await wait_for_reply(ctx, ctx.member, request)
    ctx.record("bot 回复", reply, code_entities=code_spans(reply))
    return reply


async def post_anchor(ctx: ScenarioContext, text: str) -> Msg:
    """The admin posts an ordinary message for the member to reply to."""
    msg = await ctx.admin.send(ctx.chat.id, f"{text}（E2E {ctx.run_id}）")
    ctx.record("群主发送", msg)
    ctx.plain_message_ids.append(msg.id)
    return msg


def check_rendered(reply: Msg) -> None:
    check("\\" not in reply.text, "回复中出现反斜杠，MarkdownV2 转义没有被正确解析")


def check_code(reply: Msg, value: str, what: str) -> None:
    check(value in code_spans(reply), f"{what} {value!r} 没有以行内代码显示")


# ------------------------------------------------- commands / members


async def help_scenario(ctx: ScenarioContext) -> None:
    reply = await ask(ctx, "/help")
    check(reply.text.startswith("可用命令列表："), "/help 回复不是命令列表")
    for expected in ("获取帮助信息", "用法：/help", "获取ID信息", "用法：/id"):
        check(expected in reply.text, f"/help 回复缺少 {expected!r}")


def _id_line(label: str, name: str, value: int) -> str:
    return f"{label}: {name} ({value})"


async def _check_id_reply(ctx: ScenarioContext, reply: Msg) -> list[str]:
    check_rendered(reply)
    lines = reply.text.split("\n")
    expected_chat = _id_line("当前ChatId", ctx.chat.title, ctx.chat.id)
    expected_user = _id_line("你的UserId", ctx.member_user.first_name, ctx.member_user.id)
    check(lines[:1] == [expected_chat], f"第一行应为 {expected_chat!r}")
    check(lines[1:2] == [expected_user], f"第二行应为 {expected_user!r}")
    check_code(reply, str(ctx.chat.id), "ChatId")
    check_code(reply, str(ctx.member_user.id), "UserId")
    return lines


async def id_scenario(ctx: ScenarioContext) -> None:
    lines = await _check_id_reply(ctx, await ask(ctx, "/id"))
    check(len(lines) == 2, "不回复消息时 /id 只应有两行")


async def id_reply_scenario(ctx: ScenarioContext) -> None:
    anchor = await post_anchor(ctx, "E2E /id 回复目标")
    reply = await ask(ctx, "/id", reply_to=anchor.id)
    lines = await _check_id_reply(ctx, reply)
    target = _id_line("回复目标的UserId", ctx.admin_user.first_name, ctx.admin_user.id)
    check(lines[2:3] == [target], f"第三行应为 {target!r}")
    check_code(reply, str(ctx.admin_user.id), "回复目标的 UserId")


async def _wait_deleted(ctx: ScenarioContext, message_id: int, what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while await ctx.admin.get_message(ctx.chat.id, message_id) is not None:
        if loop.time() >= deadline:
            raise CheckFailed(
                f"{what}系统消息 {message_id} 在 {ctx.reply_timeout:g} 秒内没有被删除"
            )
        await asyncio.sleep(ctx.poll_interval)
    ctx.record(f"{what}系统消息已删除", message_id=message_id)


async def service_message_scenario(ctx: ScenarioContext) -> None:
    """The member leaves and rejoins via invite link; both service messages must vanish."""
    left_id = await ctx.member.leave(ctx.chat.id)
    link = await ctx.admin.export_invite_link(ctx.chat.id)
    joined_id = await ctx.member.join_by_link(link)
    ctx.record("成员退群并重新入群", left_message_id=left_id, joined_message_id=joined_id)
    # Both halves need evidence: a missing id means "not verified", which is a failure.
    if joined_id is not None:
        await _wait_deleted(ctx, joined_id, "入群")
    if left_id is not None:
        await _wait_deleted(ctx, left_id, "退群")
    missing = [what for what, mid in (("退群", left_id), ("入群", joined_id)) if mid is None]
    if missing:
        raise CheckFailed(
            f"Telegram 没有返回{'、'.join(missing)}系统消息 id，无法验证其被删除（未验证，按失败处理）"
        )


#: The bot's private notice to someone who asked to join (fixed text, see README).
JOIN_REQUEST_TEXT = "Hello, please wait for the admin to approve your request."


async def _last_private_id(ctx: ScenarioContext) -> int:
    latest = await ctx.member.history(ctx.bot.id, min_id=0, limit=1)
    return latest[-1].id if latest else 0


async def wait_private_notice(ctx: ScenarioContext, after: int) -> Msg:
    """The bot's next message in its private chat with the member."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while True:
        for msg in await ctx.member.history(ctx.bot.id, min_id=after):
            if msg.sender_id == ctx.bot.id:
                return msg
        if loop.time() >= deadline:
            raise CheckFailed(
                f"申请入群后 {ctx.reply_timeout:g} 秒内成员没有收到 bot 的私聊等待提示"
            )
        await asyncio.sleep(ctx.poll_interval)


async def join_request_scenario(ctx: ScenarioContext) -> None:
    """Outside the group, the member asks to join through an approval link: before
    any admin decides, the bot tells the member in private to wait. The bot must
    not decide itself. Cleanup: the owner declines the request and adds the
    member back (not a link join)."""
    try:
        if await ctx.admin.participant_role(ctx.chat.id, ctx.member_user.id) is not None:
            await ctx.member.leave(ctx.chat.id)
            ctx.record("成员退群")
        after = await _last_private_id(ctx)
        link = await ctx.admin.export_request_link(ctx.chat.id)
        await ctx.member.request_join(link)
        ctx.record("成员通过需审批的邀请链接申请入群（尚未批准）")
        notice = await wait_private_notice(ctx, after)
        ctx.record("bot 私聊等待提示", notice)
        check(
            notice.text == JOIN_REQUEST_TEXT,
            f"私聊提示与约定文案不一致：{notice.text!r}（应为 {JOIN_REQUEST_TEXT!r}）",
        )
        role = await ctx.admin.participant_role(ctx.chat.id, ctx.member_user.id)
        ctx.record("检查成员是否仍在申请阶段", role=role)
        check(role is None, "管理员审批之前，成员已经在群里（bot 不应自行批准申请）")
    finally:
        try:
            await ctx.admin.decline_join_request(ctx.chat.id, ctx.member_user.id)
            ctx.record("群主拒绝入群申请")
        finally:
            await restore_member(ctx)


# ------------------------------------------------------- commands: /ip

IP_LABELS = ("IP", "Location", "AS", "Org", "ISP")
IP_SAMPLE = "1.1.1.1"


def _check_ip_reply(reply: Msg, ip: str) -> None:
    check_rendered(reply)
    fields: dict[str, str] = {}
    for line in reply.text.split("\n"):
        label, sep, value = line.partition(": ")
        if sep:
            fields[label] = value
    for label in IP_LABELS:
        value = fields.get(label, "")
        check(bool(value), f"/ip 回复缺少 {label} 字段")
        check(value != "未知", f"/ip 回复的 {label} 为“未知”")
        check_code(reply, value, label)
    check(fields["IP"] == ip, f"IP 字段应为 {ip}")


async def ip_scenario(ctx: ScenarioContext) -> None:
    _check_ip_reply(await ask(ctx, f"/ip {IP_SAMPLE}"), IP_SAMPLE)


async def ip_reply_scenario(ctx: ScenarioContext) -> None:
    anchor = await post_anchor(ctx, f"服务器地址：{IP_SAMPLE}:443")
    _check_ip_reply(await ask(ctx, "/ip", reply_to=anchor.id), IP_SAMPLE)


# ---------------------------------------------------------- moderation

CONFIG_FORBIDDEN = "需要群主或具有封禁成员权限的管理员才能使用 /config。"
WARN_DISABLED = "请先通过 /config 开启警告功能。"
WARN_LABEL = "警告"
_WARN_COUNT = re.compile(r"你已被警告 (\d+)/(\d+) 次")
_START_TOKEN = re.compile(r"[?&]start=([A-Za-z0-9_-]+)")
_MAX_WARNINGS = 10


async def admin_command(ctx: ScenarioContext, text: str, *, reply_to: int | None = None) -> Msg:
    """The admin sends ``text`` in the group and returns the bot's reply."""
    request = await ctx.admin.send(ctx.chat.id, text, reply_to=reply_to)
    ctx.record("群主发送", request)
    reply = await wait_for_reply(ctx, ctx.admin, request)
    ctx.record("bot 回复", reply)
    return reply


async def member_says(ctx: ScenarioContext, text: str) -> Msg:
    msg = await ctx.member.send(ctx.chat.id, f"{text}（E2E {ctx.run_id}）")
    ctx.record("成员发送", msg)
    return msg


async def open_config_menu(ctx: ScenarioContext) -> Msg:
    """``/config`` in the group → deep link → ``/start <token>`` in the bot's private chat."""
    entry = await admin_command(ctx, "/config")
    check(entry.text == "功能设置", f"/config 回复应为“功能设置”，实际为 {entry.text!r}")
    link = next((b.url for b in entry.buttons if b.url), None)
    check(link is not None, "/config 回复中没有跳转私聊的按钮")
    assert link is not None  # noqa: S101 - narrowed by check()
    check(
        link.startswith(f"https://t.me/{ctx.bot.username}?"),
        f"跳转链接 {link!r} 不是指向测试 bot 的深链",
    )
    token = _START_TOKEN.search(link)
    check(token is not None, f"跳转链接 {link!r} 中没有 start 参数")
    assert token is not None  # noqa: S101
    request = await ctx.admin.send(ctx.bot.id, f"/start {token.group(1)}")
    ctx.record("群主在私聊中打开深链", request, link=link)
    menu = await wait_for_reply(ctx, ctx.admin, request)
    ctx.record("bot 私聊回复", menu, buttons=[b.text for b in menu.buttons])
    check(menu.text.startswith(f"群名：{ctx.chat.title}"), f"配置菜单标题不对：{menu.text!r}")
    for label in ("反垃圾", "入群验证", WARN_LABEL, "删除系统消息"):
        check(menu.button(label) is not None, f"配置菜单缺少“{label}”开关")
    return menu


def _enabled(menu: Msg, label: str) -> bool:
    button = menu.button(label)
    check(button is not None, f"配置菜单缺少“{label}”开关")
    assert button is not None  # noqa: S101
    check(button.text[:1] in "✅❌", f"开关 {button.text!r} 没有状态标记")
    return button.text.startswith("✅")


async def toggle(ctx: ScenarioContext, menu: Msg, label: str) -> Msg:
    """Press the ``label`` switch; check the answer and the redrawn menu; return the new menu."""
    before = _enabled(menu, label)
    button = menu.button(label)
    assert button is not None  # noqa: S101 - checked by _enabled
    check(button.data is not None, f"“{label}”不是回调按钮")
    assert button.data is not None  # noqa: S101
    answer = await ctx.admin.click(menu.chat_id, menu.id, button.data)
    ctx.record(f"群主点击“{button.text}”", answer=answer)
    check(answer == "设置成功", f"点击开关后的提示应为“设置成功”，实际为 {answer!r}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while True:
        current = await ctx.admin.get_message(menu.chat_id, menu.id)
        if current is not None and _enabled(current, label) is not before:
            ctx.record("菜单已刷新", current, buttons=[b.text for b in current.buttons])
            return current
        if loop.time() >= deadline:
            raise CheckFailed(f"点击“{label}”后，菜单在 {ctx.reply_timeout:g} 秒内没有刷新为新状态")
        await asyncio.sleep(ctx.poll_interval)


async def enable_warn(ctx: ScenarioContext) -> None:
    menu = await open_config_menu(ctx)
    if not _enabled(menu, WARN_LABEL):
        await toggle(ctx, menu, WARN_LABEL)


async def wait_deleted(
    ctx: ScenarioContext, message_ids: list[int], what: str, *, within: float | None = None
) -> None:
    """Poll until the messages are gone; fail after ``within`` seconds (default ``reply_timeout``)."""
    timeout = ctx.reply_timeout if within is None else within
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    remaining = list(message_ids)
    while remaining:
        remaining = [i for i in remaining if await ctx.admin.get_message(ctx.chat.id, i)]
        if not remaining:
            break
        if loop.time() >= deadline:
            raise CheckFailed(f"{what} {remaining} 在 {timeout:g} 秒内没有被删除")
        await asyncio.sleep(ctx.poll_interval)
    ctx.record(f"{what}已全部删除", message_ids=message_ids)


async def restore_member(ctx: ScenarioContext) -> None:
    """Undo a ban of the member account: unban, then the owner adds it back.

    Not through an invite link: Telegram rate-limits an account's joins by link,
    and only captcha.verify / members.service-message need a real join. A failed add
    (``InviteFailed``) stops the run; it never falls back to a link.

    A muted member is still in the group: the mute is lifted (same call as an
    unban), never answered by adding the member again.
    """
    if await ctx.admin.is_banned(ctx.chat.id, ctx.member_user.id):
        await ctx.admin.unban(ctx.chat.id, ctx.member_user.id)
        ctx.record("群主解除成员封禁")
    elif await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id):
        await ctx.admin.unban(ctx.chat.id, ctx.member_user.id)
        ctx.record("群主解除成员禁言")
    if await ctx.admin.participant_role(ctx.chat.id, ctx.member_user.id) is None:
        await owner_add_member(ctx.admin, ctx.chat.id, ctx.member_user.id)
        ctx.record("群主把成员拉回群")


async def check_member_banned(ctx: ScenarioContext) -> None:
    banned = await ctx.admin.is_banned(ctx.chat.id, ctx.member_user.id)
    ctx.record("检查成员封禁状态", banned=banned)
    check(banned, "成员没有出现在群的封禁列表中")


def check_name_hidden(msg: Msg, ctx: ScenarioContext) -> None:
    """Public notices show only a fragment of the handled member's name.

    People advertise through their names and usernames, so the bot never repeats
    them in full in the group (AGENTS.md, "群内公开消息").

    Long names (more than 4 characters) must not appear in full, and the
    ``@username`` never appears.
    """
    name = ctx.member_user.first_name.strip()
    if len(name) > 4:
        check(name not in msg.text, "公开提醒中出现了被处理成员的完整名字")
    username = ctx.member_user.username
    if username:
        check(f"@{username}" not in msg.text, "公开提醒中出现了被处理成员的 @username")


def check_mention(reply: Msg, ctx: ScenarioContext) -> None:
    check_rendered(reply)
    check(
        reply.text.startswith(ctx.member_user.first_name.strip()[:2]),
        "回复没有以被处理成员的名字片段开头",
    )
    check(
        any(e.kind == "mention_name" for e in reply.entities),
        "被处理成员的名字没有渲染为提及链接",
    )
    check_name_hidden(reply, ctx)


async def permissions_scenario(ctx: ScenarioContext) -> None:
    """Members cannot configure or moderate; admins / the owner cannot be targeted."""
    reply = await ask(ctx, "/config")
    check(reply.text == CONFIG_FORBIDDEN, f"普通成员 /config 应被拒绝，实际回复 {reply.text!r}")
    anchor = await post_anchor(ctx, "E2E /ban 权限测试")
    reply = await ask(ctx, "/ban", reply_to=anchor.id)
    check(
        reply.text.startswith("你没有权限封禁成员"),
        f"普通成员 /ban 应被拒绝，实际回复 {reply.text!r}",
    )
    target = await ctx.admin.send(ctx.chat.id, f"群主自己的消息（E2E {ctx.run_id}）")
    ctx.plain_message_ids.append(target.id)
    reply = await admin_command(ctx, "/ban", reply_to=target.id)
    check(reply.text == "无法封禁管理员或群主。", f"对群主 /ban 应被拒绝，实际回复 {reply.text!r}")
    check(await ctx.admin.get_message(ctx.chat.id, target.id) is not None, "群主的消息被删除了")


async def config_scenario(ctx: ScenarioContext) -> None:
    """/config → private menu → toggle "警告" (ending enabled, ready for moderation.warn)."""
    menu = await open_config_menu(ctx)
    if _enabled(menu, WARN_LABEL):
        menu = await toggle(ctx, menu, WARN_LABEL)  # exercise both directions
    menu = await toggle(ctx, menu, WARN_LABEL)
    check(_enabled(menu, WARN_LABEL), "“警告”开关最终应为开启")


async def warn_scenario(ctx: ScenarioContext) -> None:
    """/warn up to the limit: counted replies, then ban + history deleted."""
    first = await member_says(ctx, "E2E 被警告的消息 1")
    second = await member_says(ctx, "E2E 被警告的消息 2")
    try:
        reply = await admin_command(ctx, "/warn", reply_to=second.id)
        if reply.text == WARN_DISABLED:
            await enable_warn(ctx)
            reply = await admin_command(ctx, "/warn", reply_to=second.id)
        for expected in range(1, _MAX_WARNINGS + 1):
            match = _WARN_COUNT.search(reply.text)
            check(match is not None, f"/warn 回复中没有警告次数：{reply.text!r}")
            assert match is not None  # noqa: S101
            count, limit = int(match.group(1)), int(match.group(2))
            check(count == expected, f"第 {expected} 次警告的回复显示为 {count}/{limit}")
            check_mention(reply, ctx)
            if count >= limit:
                check("已被封禁" in reply.text, f"达到上限后的回复没有说明已封禁：{reply.text!r}")
                break
            check(
                not await ctx.admin.is_banned(ctx.chat.id, ctx.member_user.id),
                f"警告 {count}/{limit} 次时成员就被封禁了",
            )
            reply = await admin_command(ctx, "/warn", reply_to=second.id)
        else:
            raise CheckFailed(f"警告 {_MAX_WARNINGS} 次仍未封禁")
        await check_member_banned(ctx)
        await wait_deleted(ctx, [first.id, second.id], "被封禁成员的消息")
    finally:
        await restore_member(ctx)


async def ban_scenario(ctx: ScenarioContext) -> None:
    """/ban as a reply: member banned and their recorded messages deleted."""
    sent = [await member_says(ctx, f"E2E 将被清理的消息 {i}") for i in range(1, 4)]
    try:
        reply = await admin_command(ctx, "/ban", reply_to=sent[-1].id)
        match = re.search(r"已被封禁，删除了 (\d+) 条消息", reply.text)
        check(match is not None, f"/ban 回复不符合预期：{reply.text!r}")
        assert match is not None  # noqa: S101
        check(int(match.group(1)) >= len(sent), f"/ban 只删除了 {match.group(1)} 条消息")
        check_mention(reply, ctx)
        await check_member_banned(ctx)
        await wait_deleted(ctx, [m.id for m in sent], "被封禁成员的消息")
    finally:
        await restore_member(ctx)


# ------------------------------------------------------------ antispam

ANTISPAM_LABEL = "反垃圾"
SPAM_NOTICE = "内容命中反垃圾策略"
#: An unmistakable gambling ad (the LLM must flag it).
SPAM_TEXT = (
    "【限时福利】国际正规博彩平台，注册即送888彩金，百家乐、体育、电子随便玩，"
    "提款秒到账，私聊我领取 USDT 返利"
)
#: Posted before the ad; purged with the member's history once the ad is caught.
PURGE_PROBE_TEXT = "E2E 反垃圾清理探针，普通消息"
HAM_TEXTS = ("今天天气真好啊", "周末有人一起去爬山吗", "这个版本的更新日志我看了，修了不少问题")


def require_antispam(ctx: ScenarioContext) -> None:
    if not ctx.antispam:
        raise ScenarioSkipped(
            "被测 bot 未配置 LLM（1Password staging-services/llm_api_key、llm_model），无法验收反垃圾"
        )


async def set_antispam(ctx: ScenarioContext, enabled: bool) -> None:
    menu = await open_config_menu(ctx)
    if _enabled(menu, ANTISPAM_LABEL) is not enabled:
        await toggle(ctx, menu, ANTISPAM_LABEL)
    ctx.record(f"反垃圾开关已{'开启' if enabled else '关闭'}")


async def member_posts(ctx: ScenarioContext, text: str, marker: str) -> Msg:
    """The member posts ``text`` tagged with a run-specific ``marker``."""
    tagged = f"{marker} {ctx.run_id}"
    msg = await ctx.member.send(ctx.chat.id, f"{text}（E2E {tagged}）")
    ctx.record("成员发送", msg)
    return msg


def marker_of(ctx: ScenarioContext, marker: str) -> str:
    return f"{marker} {ctx.run_id}"


async def wait_notice(ctx: ScenarioContext, after: int) -> Msg:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while True:
        for msg in await ctx.admin.history(ctx.chat.id, min_id=after):
            if msg.sender_id == ctx.bot.id and SPAM_NOTICE in msg.text:
                ctx.record("bot 发送反垃圾通知", msg)
                return msg
        if loop.time() >= deadline:
            raise CheckFailed(f"{ctx.reply_timeout:g} 秒内没有看到反垃圾通知")
        await asyncio.sleep(ctx.poll_interval)


async def wait_restricted(ctx: ScenarioContext) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while not await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id):
        if loop.time() >= deadline:
            raise CheckFailed("成员没有被禁言")
        await asyncio.sleep(ctx.poll_interval)
    ctx.record("成员已被禁言")


async def check_untouched(ctx: ScenarioContext, msg: Msg, why: str) -> None:
    """Wait ``antispam_wait`` seconds; the message must survive and the member stay free."""
    await asyncio.sleep(ctx.antispam_wait)
    still_there = await ctx.admin.get_message(ctx.chat.id, msg.id) is not None
    restricted = await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id)
    ctx.record(f"等待 {ctx.antispam_wait:g} 秒后检查", kept=still_there, restricted=restricted)
    check(still_there, f"{why}，但消息 {msg.id} 被删除了")
    check(not restricted, f"{why}，但成员被禁言了")


async def restore_unmuted(ctx: ScenarioContext) -> None:
    if await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id):
        await ctx.admin.unban(ctx.chat.id, ctx.member_user.id)
        ctx.record("群主解除成员禁言")


async def spam_scenario(ctx: ScenarioContext) -> None:
    """Switch on: an obvious ad from a new member is deleted, the member muted, a notice
    posted, and the member's earlier messages purged.

    The bot handles spam in the background: the verdict may take up to its
    classification budget, and the history purge follows ``ANTISPAM_PURGE_DELAY``
    after the mute. A message the member posted before the ad (the probe) must be
    purged too; its deletion is the last side effect, so once it is gone nothing
    more happens to the member. Whatever fails, the scenario only ends when that
    is settled (:func:`settle_spam`), so later scenarios never meet a late mute or
    purge (docs/e2e-acceptance.md, "反垃圾的异步副作用").
    """
    require_antispam(ctx)
    probe = await member_posts(ctx, PURGE_PROBE_TEXT, "probe")
    await set_antispam(ctx, True)
    loop = asyncio.get_running_loop()
    ad: Msg | None = None
    deadline = 0.0
    settled = False
    try:
        ad = await member_posts(ctx, SPAM_TEXT, "spam")
        sent_at = loop.time()
        deadline = sent_at + ctx.antispam_timeout
        ctx.classified_markers.append(marker_of(ctx, "spam"))
        await wait_deleted(ctx, [ad.id], "广告消息", within=ctx.antispam_timeout)
        ctx.record(
            "广告处理耗时",
            seconds=round(loop.time() - sent_at, 1),
            reply_timeout=ctx.reply_timeout,
            antispam_timeout=ctx.antispam_timeout,
        )
        await wait_restricted(ctx)
        notice = await wait_notice(ctx, ad.id)
        await wait_deleted(
            ctx, [probe.id], "成员此前的消息（反垃圾清理历史）", within=purge_window(ctx)
        )
        settled = True
        check_rendered(notice)
        check(
            any(e.kind == "mention_name" for e in notice.entities),
            "反垃圾通知中没有提及被处理的成员",
        )
        check_name_hidden(notice, ctx)
    finally:
        await finish_spam(ctx, ad, probe, deadline, settled=settled)


async def finish_spam(
    ctx: ScenarioContext, ad: Msg | None, probe: Msg, deadline: float, *, settled: bool
) -> None:
    """Cleanup of ``antispam.enabled``: settle the bot's side effects, then lift the mute.

    Anything that keeps us from confirming both — a missing purge, but also a
    failed read while checking or a failed unmute — raises
    :class:`SideEffectsUnsettled`, so the run stops instead of running the next
    scenario next to a muted member or a pending purge.
    """
    problems: list[str] = []
    unsettled: SideEffectsUnsettled | None = None
    if ad is not None and not settled:
        try:
            await settle_spam(ctx, ad, probe, deadline)
        except SideEffectsUnsettled as exc:
            unsettled = exc
        except Exception as exc:
            problems.append(f"确认 bot 的处理是否结束时出错（{type(exc).__name__}: {exc}）")
    try:
        await restore_unmuted(ctx)
    except Exception as exc:
        problems.append(f"无法确认成员已解除禁言（{type(exc).__name__}: {exc}）")
    if problems:
        prefix = f"{unsettled}；" if unsettled is not None else "反垃圾的异步副作用未确认结束："
        unsettled = SideEffectsUnsettled(prefix + "；".join(problems))
    if unsettled is not None:
        # Raised here, outside the handlers above, so that ``__context__`` stays
        # the scenario's own failure (if any) and the report keeps it.
        raise unsettled


def purge_window(ctx: ScenarioContext) -> float:
    """How long after the mute the history purge may take to show up."""
    return ctx.antispam_purge_delay + ctx.reply_timeout


async def settle_spam(ctx: ScenarioContext, ad: Msg, probe: Msg, deadline: float) -> None:
    """After a failed ``antispam.enabled``: make sure the bot will not act on the member later.

    Settled when either the bot never acted on the ad within its budget
    (``deadline``; it then lets the message pass), or it acted and the history
    purge (the probe's deletion) followed. Otherwise raise
    :class:`SideEffectsUnsettled` so that the run stops.
    """
    loop = asyncio.get_running_loop()
    member = ctx.member_user.id
    while True:
        deleted = await ctx.admin.get_message(ctx.chat.id, ad.id) is None
        acted = deleted or await ctx.admin.is_restricted(ctx.chat.id, member)
        if acted or loop.time() >= deadline:
            break
        await asyncio.sleep(ctx.poll_interval)
    if not acted:
        ctx.record(
            f"等待 {ctx.antispam_timeout:g} 秒（被测 bot 的判定预算）后，bot 没有处理广告；"
            "超出预算的判定按放行处理，不会再有迟到的删除、禁言或清理",
            ad_kept=True,
        )
        return
    ctx.record("bot 已处理广告（迟到或不完整），等待其后续清理结束", ad_deleted=deleted)
    purge_deadline = loop.time() + purge_window(ctx)
    while await ctx.admin.get_message(ctx.chat.id, probe.id) is not None:
        if loop.time() >= purge_deadline:
            raise SideEffectsUnsettled(
                f"反垃圾的异步副作用未确认结束：bot 已处理广告，但 {purge_window(ctx):g} 秒内"
                f"没有看到成员历史消息 {probe.id} 被清理，后续场景可能被迟到的清理影响"
            )
        await asyncio.sleep(ctx.poll_interval)
    ctx.record("反垃圾的迟到处理已结束（成员历史消息已清理）", message_id=probe.id)


async def disabled_scenario(ctx: ScenarioContext) -> None:
    """Switch off: the same ad is left alone."""
    require_antispam(ctx)
    await set_antispam(ctx, False)
    ad = await member_posts(ctx, SPAM_TEXT, "off")
    try:
        await check_untouched(ctx, ad, "反垃圾已关闭")
    finally:
        await restore_unmuted(ctx)


async def exempt_scenario(ctx: ScenarioContext) -> None:
    """Members with more than 3 messages are exempt, even when posting an ad."""
    require_antispam(ctx)
    await set_antispam(ctx, True)
    try:
        for i, text in enumerate(HAM_TEXTS):
            await member_posts(ctx, text, f"ham{i}")
        ad = await member_posts(ctx, SPAM_TEXT, "exempt")
        await check_untouched(ctx, ad, "老成员应被豁免")
    finally:
        await restore_unmuted(ctx)
        await set_antispam(ctx, False)


async def spam_command_scenario(ctx: ScenarioContext) -> None:
    """/spam by the owner bans the reported member; members cannot report the owner."""
    anchor = await post_anchor(ctx, "E2E /spam 举报测试")
    reply = await ask(ctx, "/spam", reply_to=anchor.id)
    check(reply.text == "无法举报管理员或群主。", f"举报群主应被拒绝，实际回复 {reply.text!r}")
    target = await member_says(ctx, "E2E 被举报的消息")
    try:
        reply = await admin_command(ctx, "/spam", reply_to=target.id)
        check("已被封禁（垃圾信息）" in reply.text, f"/spam 回复不符合预期：{reply.text!r}")
        check_mention(reply, ctx)
        await check_member_banned(ctx)
        await wait_deleted(ctx, [target.id], "被举报成员的消息")
    finally:
        await restore_member(ctx)


async def langfuse_scenario(ctx: ScenarioContext) -> None:
    """Every classification of this run is visible in Langfuse."""
    require_antispam(ctx)
    if ctx.traces is None:
        raise ScenarioSkipped("未配置 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY，无法查询 Langfuse")
    if not ctx.classified_markers:
        raise ScenarioSkipped(
            "本次运行没有需要核对的判定（先运行 antispam.enabled）", reason="not_selected"
        )
    counts = await ctx.traces.wait_for_markers(ctx.classified_markers, since=ctx.started_at)
    ctx.record("查询 Langfuse generation", counts=counts)
    missing = [marker for marker, count in counts.items() if not count]
    check(not missing, f"Langfuse 中没有这些判定的记录：{missing}")


# ------------------------------------------------------------- captcha

CAPTCHA_LABEL = "入群验证"
CAPTCHA_PROMPT_BUTTON = "开始入群验证"
CAPTCHA_START_TEXT = "点击下方按钮完成入群验证"
#: Placeholder sent as the Turnstile token. A real token needs a person to solve
#: the challenge in a browser; the API must reject this one.
UNSOLVED_TURNSTILE_TOKEN = "e2e-unsolved-turnstile-token"  # noqa: S105 - not a credential
_CHALLENGE = re.compile(
    r"/captcha\?challenge=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
TURNSTILE_NOT_AUTOMATED = (
    "captcha.verify：真实 Turnstile 挑战的完成与随后的解除禁言需要真人在浏览器中完成，"
    "工具不求解挑战、也不使用测试 key；本次只验证到“未通过 Turnstile 的提交被拒绝且成员保持禁言”"
)


def require_web(ctx: ScenarioContext) -> tuple[WebProbe, str]:
    if ctx.web is None or ctx.api_base_url is None:
        raise ScenarioSkipped(
            "未配置 E2E_API_BASE_URL（被测环境的公开 API），无法验收 Mini App 链路"
        )
    return ctx.web, ctx.api_base_url


async def set_captcha(ctx: ScenarioContext, enabled: bool) -> None:
    menu = await open_config_menu(ctx)
    if _enabled(menu, CAPTCHA_LABEL) is not enabled:
        await toggle(ctx, menu, CAPTCHA_LABEL)
    ctx.record(f"入群验证开关已{'开启' if enabled else '关闭'}")


async def wait_captcha_prompt(ctx: ScenarioContext, after: int) -> str:
    """The bot's group prompt for the member; returns its ``start`` payload."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while True:
        for msg in await ctx.admin.history(ctx.chat.id, min_id=after):
            button = msg.button(CAPTCHA_PROMPT_BUTTON)
            if msg.sender_id == ctx.bot.id and button is not None and button.url:
                ctx.record("bot 在群内发送验证提示", msg, link=button.url)
                # The bot masks the display name but links it to the member (text mention).
                check(
                    any(e.kind == "mention_name" for e in msg.entities),
                    "验证提示没有以提及链接的形式提到入群成员",
                )
                token = _START_TOKEN.search(button.url)
                check(
                    button.url.startswith(f"https://t.me/{ctx.bot.username}?")
                    and token is not None,
                    f"验证按钮 {button.url!r} 不是指向被测 bot 的深链",
                )
                assert token is not None  # noqa: S101 - narrowed by check()
                return token.group(1)
        if loop.time() >= deadline:
            raise CheckFailed(f"成员入群后 {ctx.reply_timeout:g} 秒内没有看到 bot 的验证提示")
        await asyncio.sleep(ctx.poll_interval)


def launch_init_data(launch_url: str) -> str:
    """The raw ``tgWebAppData`` (initData) from a Mini App launch URL fragment."""
    fragment = urllib.parse.urlsplit(launch_url).fragment
    values = urllib.parse.parse_qs(fragment, keep_blank_values=True).get("tgWebAppData", [])
    return values[0] if values else ""


async def captcha_scenario(ctx: ScenarioContext) -> None:
    """Join → muted → prompt → private /start → Mini App → API, as the member really sees it."""
    web, api = require_web(ctx)
    await set_captcha(ctx, True)
    try:
        if await ctx.admin.participant_role(ctx.chat.id, ctx.member_user.id) is not None:
            await ctx.member.leave(ctx.chat.id)
            ctx.record("成员退群")
        anchor = await post_anchor(ctx, "E2E 入群验证开始")
        link = await ctx.admin.export_invite_link(ctx.chat.id)
        await ctx.member.join_by_link(link)
        ctx.record("成员通过邀请链接入群")
        token = await wait_captcha_prompt(ctx, anchor.id)
        await wait_restricted(ctx)

        request = await ctx.member.send(ctx.bot.id, f"/start {token}")
        ctx.record("成员在私聊中打开验证深链", request)
        reply = await wait_for_reply(ctx, ctx.member, request)
        ctx.record("bot 私聊回复", reply, buttons=[b.text for b in reply.buttons])
        check(CAPTCHA_START_TEXT in reply.text, f"私聊回复不是验证入口：{reply.text!r}")
        button = next((b for b in reply.buttons if b.web_app and b.url), None)
        check(button is not None, "私聊回复中没有打开 Mini App 的 web_app 按钮")
        assert button is not None  # noqa: S101 - narrowed by check()
        assert button.url is not None  # noqa: S101
        page_url = button.url
        match = _CHALLENGE.search(page_url)
        check(match is not None, f"Mini App 地址 {page_url!r} 不是 /captcha?challenge=<uuid>")
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(page_url))
        if ctx.webapp_url is not None:
            check(origin == ctx.webapp_url, f"Mini App 来源 {origin} 不是预期的 {ctx.webapp_url}")

        launch = await ctx.member.open_web_app(ctx.bot, page_url)
        init_data = launch_init_data(launch)
        fields = urllib.parse.parse_qs(init_data)
        user = json.loads(fields.get("user", ["{}"])[0] or "{}")
        ctx.record(
            "Telegram 签发 Mini App 启动参数",
            init_data_length=len(init_data),
            init_data_fields=sorted(fields),
        )
        check("hash" in fields and "auth_date" in fields, "启动参数中没有签名过的 initData")
        check(user.get("id") == ctx.member_user.id, "initData 中的用户不是入群成员")

        page = await web.get(page_url)
        ctx.record(
            "打开 Mini App 页面", status=page.status, cache=page.headers.get("cache-control")
        )
        check(page.status == 200, f"Mini App 页面返回 HTTP {page.status}")
        check('<div id="root">' in page.text, "Mini App 页面不是预期的单页应用")

        body = {
            "challenge_id": match.group(1) if match else "",
            "init_data": init_data,
            "turnstile_token": UNSOLVED_TURNSTILE_TOKEN,
        }
        result = await web.post_json(f"{api}/api/v1/captcha/verify", body, origin=origin)
        answer = result.json() or {}
        ctx.record(
            "以未通过 Turnstile 的 token 提交验证",
            status=result.status,
            error=answer.get("error"),
            cors=result.headers.get("access-control-allow-origin"),
        )
        check(
            result.headers.get("access-control-allow-origin") == origin,
            f"API 没有对 Mini App 来源 {origin} 放行 CORS",
        )
        check(
            result.status == 400 and answer.get("error") == "turnstile_failed",
            f"未通过 Turnstile 的提交应返回 400 turnstile_failed，实际为 {result.status} {answer}",
        )
        still = await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id)
        ctx.record("提交被拒绝后检查成员状态", restricted=still)
        check(still, "Turnstile 未通过，成员却被解除了禁言")
        ctx.not_automated.append(TURNSTILE_NOT_AUTOMATED)
    finally:
        # Switch the captcha off first so that restoring the member cannot mute it again.
        try:
            await set_captcha(ctx, False)
        finally:
            await restore_member(ctx)
            await restore_unmuted(ctx)


#: What the page shows when the Turnstile widget itself fails (always-fail site key).
WIDGET_ERROR_TEXT = "人机验证组件加载失败"
#: The API's message for ``turnstile_failed`` (Siteverify rejected the token).
SITEVERIFY_REJECTED_TEXT = "人机验证未通过"
TEST_KEYS_NOTE = (
    "captcha.test-keys：使用 Cloudflare 官方 Turnstile 测试 key，在本地构建的 Mini App "
    "和本机被测 bot 上验证业务链路；它证明 widget → 提交 → Siteverify → 解除/保持禁言的链路，"
    "不证明真实挑战可被真人通过，不能替代 staging 的真实 widget 证据"
)


def require_turnstile_lab(ctx: ScenarioContext) -> TurnstileLab:
    if ctx.turnstile_lab is None:
        raise ScenarioSkipped(
            "仅在本地 --serve 并显式选择 captcha.test-keys 时运行（需要本地 Mini App 与浏览器）"
        )
    return ctx.turnstile_lab


def _on_origin(url: str, origin: str) -> str:
    """``url`` (path, query and Telegram's launch fragment) served from ``origin``."""
    parts = urllib.parse.urlsplit(url)
    target = urllib.parse.urlsplit(origin)
    return urllib.parse.urlunsplit(
        (target.scheme, target.netloc, parts.path, parts.query, parts.fragment)
    )


async def _visit(ctx: ScenarioContext, lab: TurnstileLab, step: str, url: str) -> MiniAppVisit:
    visit = await lab.visit(url)
    ctx.record(
        step,
        origin="{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url)),
        siteverify_secret=lab.siteverify,
        page_phase=visit.phase,
        page_text=visit.text,
        verify_calls=[{"status": status, "error": error} for status, error in visit.verify_calls],
        client_events=list(visit.client_events),
    )
    return visit


async def _check_still_muted(ctx: ScenarioContext, why: str) -> None:
    still = await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id)
    ctx.record(f"{why}后检查成员状态", restricted=still)
    check(still, f"{why}，成员却被解除了禁言")


async def wait_unrestricted(ctx: ScenarioContext) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while await ctx.admin.is_restricted(ctx.chat.id, ctx.member_user.id):
        if loop.time() >= deadline:
            raise CheckFailed(f"验证通过后 {ctx.reply_timeout:g} 秒内成员仍被禁言")
        await asyncio.sleep(ctx.poll_interval)
    ctx.record("成员已被解除禁言")


async def captcha_test_keys_scenario(ctx: ScenarioContext) -> None:
    """Official Turnstile test keys, real widget in a browser, real Telegram (local only).

    One join, three attempts on the same challenge:

    1. always-fail **site key**: the widget errors, the page never calls the API;
    2. always-pass site key + always-fail **secret key**: the widget yields the
       dummy token, Siteverify rejects it, the API answers ``turnstile_failed``;
    3. always-pass site key + always-pass secret key: verification passes and
       the bot lifts the mute.
    """
    lab = require_turnstile_lab(ctx)
    ctx.test_keys.extend([TEST_KEYS_NOTE, *lab.describe()])
    await set_captcha(ctx, True)
    try:
        await restore_unmuted(ctx)
        if await ctx.admin.participant_role(ctx.chat.id, ctx.member_user.id) is not None:
            await ctx.member.leave(ctx.chat.id)
            ctx.record("成员退群")
        anchor = await post_anchor(ctx, "E2E 入群验证（官方测试 key）开始")
        # The owner adds the member back: a join like any other for the captcha,
        # without spending one of the member's rate-limited link joins.
        await owner_add_member(ctx.admin, ctx.chat.id, ctx.member_user.id)
        ctx.record("群主把成员拉回群")
        token = await wait_captcha_prompt(ctx, anchor.id)
        await wait_restricted(ctx)

        request = await ctx.member.send(ctx.bot.id, f"/start {token}")
        ctx.record("成员在私聊中打开验证深链", request)
        reply = await wait_for_reply(ctx, ctx.member, request)
        button = next((b for b in reply.buttons if b.web_app and b.url), None)
        check(button is not None, "私聊回复中没有打开 Mini App 的 web_app 按钮")
        assert button is not None  # noqa: S101 - narrowed by check()
        assert button.url is not None  # noqa: S101
        page_url = button.url
        check(
            _CHALLENGE.search(page_url) is not None,
            f"Mini App 地址 {page_url!r} 不是 /captcha?challenge=<uuid>",
        )
        check(
            page_url.startswith(f"{lab.pass_origin}/"),
            f"Mini App 地址 {page_url!r} 不是本地测试 Mini App {lab.pass_origin}",
        )
        launch = await ctx.member.open_web_app(ctx.bot, page_url)
        init_data = launch_init_data(launch)
        ctx.record("Telegram 签发 Mini App 启动参数", init_data_length=len(init_data))
        check(bool(init_data), "启动地址中没有 initData")

        # 1. Widget fails: nothing reaches the API.
        if lab.siteverify != "fail":
            await lab.use_secret("fail")
        visit = await _visit(
            ctx,
            lab,
            "固定失败 site key：打开 Mini App",
            _on_origin(launch, lab.widget_fail_origin),
        )
        check(
            visit.phase == "failed" and WIDGET_ERROR_TEXT in visit.text,
            f"固定失败的 widget 应显示“{WIDGET_ERROR_TEXT}”，实际为 {visit.phase} {visit.text!r}",
        )
        check(not visit.verify_calls, f"widget 失败时页面不应调用验证接口：{visit.verify_calls}")
        await _check_still_muted(ctx, "widget 失败")

        # 2. Widget passes, Siteverify rejects.
        visit = await _visit(ctx, lab, "固定失败 secret key：打开 Mini App", launch)
        check(
            visit.verify_calls == ((400, "turnstile_failed"),),
            f"Siteverify 拒绝时接口应返回一次 400 turnstile_failed，实际为 {visit.verify_calls}",
        )
        check(
            visit.phase == "failed" and SITEVERIFY_REJECTED_TEXT in visit.text,
            f"页面应显示“{SITEVERIFY_REJECTED_TEXT}”，实际为 {visit.phase} {visit.text!r}",
        )
        await _check_still_muted(ctx, "Siteverify 拒绝")

        # 3. Both pass: the member is unmuted.
        await lab.use_secret("pass")
        ctx.record("被测 bot 以固定通过的 secret key 重启")
        visit = await _visit(ctx, lab, "固定通过 secret key：打开 Mini App", launch)
        check(
            visit.verify_calls == ((200, None),),
            f"验证通过时接口应返回一次 200，实际为 {visit.verify_calls}",
        )
        check(visit.phase == "passed", f"页面没有显示验证成功：{visit.phase} {visit.text!r}")
        check("web_app_close" in visit.client_events, "验证成功后页面没有关闭 Mini App")
        await wait_unrestricted(ctx)
    finally:
        try:
            await set_captcha(ctx, False)
        finally:
            await restore_member(ctx)
            await restore_unmuted(ctx)


SCENARIOS: tuple[Scenario, ...] = (
    # Antispam first: the spam check only applies to members with at most 3 recorded
    # messages, and every scenario below makes the member post more.
    Scenario(
        "antispam.enabled", "开启反垃圾后，新成员的明显广告被删除并禁言，发出通知", spam_scenario
    ),
    Scenario("antispam.disabled", "关闭反垃圾后，同样的广告不再处理", disabled_scenario),
    Scenario("antispam.exempt", "发言超过 3 条的老成员发广告被豁免", exempt_scenario),
    Scenario("antispam.langfuse", "每次判定都能在 Langfuse 中查到", langfuse_scenario),
    Scenario("commands.help", "/help 返回命令列表", help_scenario),
    Scenario("commands.id", "/id 返回群 ID 与发送者 ID（行内代码）", id_scenario),
    Scenario("commands.id-reply", "回复群主消息的 /id 额外显示被回复者 ID", id_reply_scenario),
    Scenario("commands.ip", "/ip 1.1.1.1 返回五个字段且 MarkdownV2 渲染正确", ip_scenario),
    Scenario("commands.ip-reply", "回复带标签 IP 的消息执行 /ip", ip_reply_scenario),
    Scenario(
        "moderation.permissions",
        "普通成员 /config、/ban 被拒绝；不能封禁群主",
        permissions_scenario,
    ),
    Scenario("moderation.config", "/config 深链进入私聊，点击开关切换“警告”", config_scenario),
    # moderation.warn / moderation.ban ban the member, then unban it and the owner adds it back.
    Scenario("moderation.warn", "/warn 计数，达到上限自动封禁并删除历史消息", warn_scenario),
    Scenario("moderation.ban", "/ban 封禁成员并删除其历史消息", ban_scenario),
    Scenario(
        "antispam.spam-command",
        "成员不能举报群主；群主 /spam 直接封禁并删除历史消息",
        spam_command_scenario,
    ),
    Scenario(
        "captcha.verify",
        "入群被禁言 → 群内提示 → 私聊深链 → Mini App（真实签名 initData）→ 验证接口与 CORS",
        captcha_scenario,
    ),
    # The member leaves, asks to join through an approval link; the owner
    # declines and adds it back.
    Scenario(
        "members.join-request",
        "成员通过需审批的链接申请入群，批准前收到 bot 的私聊等待提示（bot 不自行审批）",
        join_request_scenario,
    ),
    # Last: it makes the member leave and rejoin the group.
    Scenario(
        "members.service-message", "成员退群、入群的系统消息被 bot 删除", service_message_scenario
    ),
)


#: Scenarios that only run against a bot started by ``run --serve`` and only
#: when named explicitly: never part of a default or group selection, never
#: part of the staging release catalog.
TEST_KEYS_SCENARIO = "captcha.test-keys"
LOCAL_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        TEST_KEYS_SCENARIO,
        "（仅本地 --serve，官方 Turnstile 测试 key）widget 固定失败、Siteverify 拒绝时保持禁言；"
        "通过时解除禁言",
        captcha_test_keys_scenario,
    ),
)


def catalog(*, serve: bool) -> tuple[Scenario, ...]:
    """Every scenario a run can report on: local-only ones need ``--serve``."""
    return SCENARIOS + LOCAL_SCENARIOS if serve else SCENARIOS


def groups() -> list[str]:
    """Selectable groups, in catalog order."""
    return list(dict.fromkeys(s.group for s in SCENARIOS))


def select_scenarios(
    selectors: list[str] | None,
    *,
    serve: bool = False,
) -> list[Scenario]:
    """Select by group (``moderation``) or exact name (``commands.ip``); default: all.

    Local-only scenarios (:data:`LOCAL_SCENARIOS`) are selected by exact name
    only, and only with ``serve``.
    """
    if not selectors:
        return list(SCENARIOS)
    wanted: set[str] = set()
    for selector in selectors:
        for raw in selector.split(","):
            item = raw.strip().lower()
            if not item:
                continue
            wanted.add(item)
    local = {s.name for s in LOCAL_SCENARIOS}
    if wanted & local and not serve:
        raise ValueError(f"{', '.join(sorted(wanted & local))} 只能与 --serve 一起使用")
    known = {s.name for s in SCENARIOS} | set(groups()) | local
    unknown = wanted - known
    if unknown:
        raise ValueError(
            f"未知场景：{', '.join(sorted(unknown))}（可用分组：{', '.join(groups())}；"
            "场景名见 list 命令）"
        )
    return [s for s in SCENARIOS if s.name in wanted or s.group in wanted] + [
        s for s in LOCAL_SCENARIOS if s.name in wanted
    ]
