"""Bot commands. :data:`COMMANDS` is the single declarative list."""

from __future__ import annotations

from telegram.ext import filters

from akkiabot_server.bot.commands.basic import help_command, id_command, start_command
from akkiabot_server.bot.commands.ip import IP_DOCS, IP_USAGE, ip_command
from akkiabot_server.bot.registry import Command

COMMANDS: tuple[Command, ...] = (
    Command(
        "start",
        "开始使用机器人",
        start_command,
        public=False,
        filters=filters.ChatType.PRIVATE,
    ),
    Command(
        "help",
        "获取帮助信息",
        help_command,
        usage="/help [命令名]",
        docs="用法：/help [命令名]\n不带参数时列出全部命令，带命令名时显示该命令的详细用法。",
    ),
    Command(
        "id",
        "获取ID信息",
        id_command,
        usage="/id（回复一条消息时，同时显示被回复者的 ID）",
    ),
    Command("ip", "查询IP信息", ip_command, usage=IP_USAGE, docs=IP_DOCS),
)

__all__ = ["COMMANDS"]
