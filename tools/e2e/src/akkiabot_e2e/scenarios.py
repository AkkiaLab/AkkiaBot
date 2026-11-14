"""M2 / M3 acceptance scenarios, driven by real user accounts.

Each scenario sends messages as a test user, waits for the bot's reply and
checks what a user actually sees: the rendered text (MarkdownV2 must be
consumed, i.e. no escape backslashes) and the code entities around values.
"""

from __future__ import annotations

import asyncio
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
    """Poll until the bot replies to ``request``; fail after ``ctx.reply_timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ctx.reply_timeout
    while True:
        for msg in await account.history(ctx.chat.id, min_id=request.id):
            if msg.sender_id == ctx.bot.id and msg.reply_to_id == request.id:
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
    if joined_id is None:
        raise CheckFailed("Telegram 没有返回入群系统消息 id，无法验证删除")
    await _wait_deleted(ctx, joined_id, "入群")
    if left_id is not None:
        await _wait_deleted(ctx, left_id, "退群")


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


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("m2.help", "M2", "/help 返回命令列表", help_scenario),
    Scenario("m2.id", "M2", "/id 返回群 ID 与发送者 ID（行内代码）", id_scenario),
    Scenario("m2.id-reply", "M2", "回复群主消息的 /id 额外显示被回复者 ID", id_reply_scenario),
    Scenario("m3.ip", "M3", "/ip 1.1.1.1 返回五个字段且 MarkdownV2 渲染正确", ip_scenario),
    Scenario("m3.ip-reply", "M3", "回复带标签 IP 的消息执行 /ip", ip_reply_scenario),
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
