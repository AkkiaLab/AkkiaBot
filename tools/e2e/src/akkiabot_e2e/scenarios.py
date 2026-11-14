"""M2 / M3 / M4 acceptance scenarios, driven by real user accounts.

Each scenario sends messages as a test user, waits for the bot's reply and
checks what a user actually sees: the rendered text (MarkdownV2 must be
consumed, i.e. no escape backslashes) and the code entities around values.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from akkiabot_e2e.errors import CheckFailed
from akkiabot_e2e.transport import Account, ChatRef, Msg, UserInfo, code_spans

Status = Literal["passed", "failed", "error", "not_run"]


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
    milestone: str
    description: str
    run: ScenarioFn


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


# ------------------------------------------------------------------ M2


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


# ------------------------------------------------------------------ M3

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


# ------------------------------------------------------------------ M4

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


async def wait_deleted(ctx: ScenarioContext, message_ids: list[int], what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    remaining = list(message_ids)
    while remaining:
        remaining = [i for i in remaining if await ctx.admin.get_message(ctx.chat.id, i)]
        if not remaining:
            break
        if loop.time() >= deadline:
            raise CheckFailed(f"{what} {remaining} 在 {ctx.reply_timeout:g} 秒内没有被删除")
        await asyncio.sleep(ctx.poll_interval)
    ctx.record(f"{what}已全部删除", message_ids=message_ids)


async def restore_member(ctx: ScenarioContext) -> None:
    """Undo a ban of the member account: unban and rejoin through a one-time link."""
    if await ctx.admin.is_banned(ctx.chat.id, ctx.member_user.id):
        await ctx.admin.unban(ctx.chat.id, ctx.member_user.id)
        ctx.record("群主解除成员封禁")
    if await ctx.admin.participant_role(ctx.chat.id, ctx.member_user.id) is None:
        link = await ctx.admin.export_invite_link(ctx.chat.id)
        joined = await ctx.member.join_by_link(link)
        ctx.record("成员重新入群", joined_message_id=joined)


async def check_member_banned(ctx: ScenarioContext) -> None:
    banned = await ctx.admin.is_banned(ctx.chat.id, ctx.member_user.id)
    ctx.record("检查成员封禁状态", banned=banned)
    check(banned, "成员没有出现在群的封禁列表中")


def check_mention(reply: Msg, ctx: ScenarioContext) -> None:
    check_rendered(reply)
    check(reply.text.startswith(ctx.member_user.first_name), "回复没有以被处理成员的名字开头")
    check(
        any(e.kind == "mention_name" for e in reply.entities),
        "被处理成员的名字没有渲染为提及链接",
    )


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
    """/config → private menu → toggle "警告" (ending enabled, ready for m4.warn)."""
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


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("m2.help", "M2", "/help 返回命令列表", help_scenario),
    Scenario("m2.id", "M2", "/id 返回群 ID 与发送者 ID（行内代码）", id_scenario),
    Scenario("m2.id-reply", "M2", "回复群主消息的 /id 额外显示被回复者 ID", id_reply_scenario),
    Scenario("m3.ip", "M3", "/ip 1.1.1.1 返回五个字段且 MarkdownV2 渲染正确", ip_scenario),
    Scenario("m3.ip-reply", "M3", "回复带标签 IP 的消息执行 /ip", ip_reply_scenario),
    Scenario(
        "m4.permissions",
        "M4",
        "普通成员 /config、/ban 被拒绝；不能封禁群主",
        permissions_scenario,
    ),
    Scenario("m4.config", "M4", "/config 深链进入私聊，点击开关切换“警告”", config_scenario),
    # m4.warn / m4.ban ban the member and then unban + re-invite it.
    Scenario("m4.warn", "M4", "/warn 计数，达到上限自动封禁并删除历史消息", warn_scenario),
    Scenario("m4.ban", "M4", "/ban 封禁成员并删除其历史消息", ban_scenario),
    # Last: it makes the member leave and rejoin the group.
    Scenario(
        "m2.service-message", "M2", "成员退群、入群的系统消息被 bot 删除", service_message_scenario
    ),
)


def select_scenarios(selectors: list[str] | None) -> list[Scenario]:
    """Select by milestone (``m2``) or exact name (``m3.ip``); default: all."""
    if not selectors:
        return list(SCENARIOS)
    wanted = {s.strip().lower() for selector in selectors for s in selector.split(",") if s.strip()}
    known = {s.name for s in SCENARIOS} | {s.milestone.lower() for s in SCENARIOS}
    unknown = wanted - known
    if unknown:
        raise ValueError(f"未知场景：{', '.join(sorted(unknown))}")
    return [s for s in SCENARIOS if s.name in wanted or s.milestone.lower() in wanted]
