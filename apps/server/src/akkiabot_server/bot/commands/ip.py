"""``/ip``: whois lookup of an address given as argument or found in a replied message."""

from __future__ import annotations

import math

from telegram import Update
from telegram.constants import ParseMode

from akkiabot_core.telegram import escape_markdown_v2_code
from akkiabot_server.bot.commands.basic import replied_message
from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.types import Context
from akkiabot_server.services.ip_lookup import IpQueryResult, IpQueryStatus
from akkiabot_whois import IpInfo

IP_USAGE = "/ip <IP 地址>（或回复一条包含 IP 地址的消息）"
IP_DOCS = f"用法：{IP_USAGE}\n查询 IPv4 / IPv6 公网地址的位置、AS、Org、ISP 信息。\n例：/ip 1.1.1.1"
_UNKNOWN = "未知"


def format_ip_info(info: IpInfo) -> str:
    """MarkdownV2 text: one ``Label: `value``` line per field."""
    rows = (
        ("IP", info.ip),
        ("Location", info.location),
        ("AS", info.asn),
        ("Org", info.org),
        ("ISP", info.isp),
    )
    return "\n".join(
        f"{label}: `{escape_markdown_v2_code(value or _UNKNOWN)}`" for label, value in rows
    )


def _plain_reply(result: IpQueryResult) -> str:
    match result.status:
        case IpQueryStatus.USAGE:
            return IP_DOCS
        case IpQueryStatus.INVALID_IP:
            return f"IP 地址格式错误。\n{IP_DOCS}"
        case IpQueryStatus.NO_IP:
            return "被回复的消息中没有找到 IP 地址。"
        case IpQueryStatus.NOT_PUBLIC:
            return f"{result.ip} 不是公网地址，无法查询。"
        case IpQueryStatus.RATE_LIMITED:
            return f"查询太频繁，请 {math.ceil(result.retry_after)} 秒后再试。"
        case IpQueryStatus.FAILED | IpQueryStatus.OK:
            return "查询失败，请稍后再试。"


async def ip_command(update: Update, context: Context) -> str | None:
    message = update.effective_message
    if message is None:
        return "ignored"
    user = update.effective_user
    # Anonymous admins / channels share one placeholder user: limit them per sender chat.
    sender = message.sender_chat.id if message.sender_chat else (user.id if user else 0)

    reply = replied_message(message)
    replied_text = (reply.text or reply.caption) if reply else None
    argument = context.args[0] if context.args else None

    result = await get_deps(context).ip_lookup.query(sender, argument, replied_text)
    if result.status is IpQueryStatus.OK and result.info is not None:
        await message.reply_text(format_ip_info(result.info), parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await message.reply_text(_plain_reply(result))
    return result.status.value
