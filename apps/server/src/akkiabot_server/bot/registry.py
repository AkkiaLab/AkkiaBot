"""Declarative command registry.

One list of :class:`Command` drives everything: PTB ``CommandHandler``s, the
``set_my_commands`` menu and the ``/help`` text, so they cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import BaseHandler, CommandHandler, filters

from akkiabot_core.observability import count_command
from akkiabot_server.bot.instrumentation import instrument
from akkiabot_server.bot.types import Context

# Telegram: 1-32 chars, lowercase letters, digits and underscores.
_NAME = re.compile(r"[a-z0-9_]{1,32}")

type CommandCallback = Callable[[Update, Context], Awaitable[str | None]]
"""A command callback. It may return a result label for ``command_total``
(e.g. ``"invalid_payload"``); ``None`` means ``"ok"``."""


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    callback: CommandCallback
    usage: str = ""
    """Usage line shown by ``/help``; defaults to ``/<name>``."""
    docs: str = ""
    """Long help shown by ``/help <name>``; defaults to the usage line."""
    public: bool = True
    """Listed in the Telegram command menu and in ``/help``."""
    filters: filters.BaseFilter | None = field(default=None)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"非法命令名：{self.name!r}")
        if not 3 <= len(self.description) <= 256:
            raise ValueError(f"命令 {self.name} 的描述长度必须为 3-256")

    @property
    def usage_line(self) -> str:
        return self.usage or f"/{self.name}"


class CommandRegistry:
    def __init__(self, commands: Iterable[Command] = ()) -> None:
        self._commands: dict[str, Command] = {}
        for command in commands:
            self.add(command)

    def add(self, command: Command) -> Command:
        if command.name in self._commands:
            raise ValueError(f"命令 /{command.name} 重复注册")
        self._commands[command.name] = command
        return command

    def __iter__(self) -> Iterator[Command]:
        return iter(self._commands.values())

    def get(self, name: str) -> Command | None:
        return self._commands.get(name.removeprefix("/").lower())

    @property
    def public(self) -> list[Command]:
        return [c for c in self if c.public]

    def bot_commands(self) -> list[BotCommand]:
        """Payload for ``set_my_commands``."""
        return [BotCommand(c.name, c.description) for c in self.public]

    def help_text(self) -> str:
        lines = ["可用命令列表："]
        for index, command in enumerate(self.public, start=1):
            lines.append(f"{index}. {command.description}")
            lines.append(f"用法：{command.usage_line}")
        return "\n".join(lines)

    def command_help(self, name: str) -> str:
        command = self.get(name)
        if command is None or not command.public:
            return f"命令 {name} 不存在"
        return command.docs or f"用法：{command.usage_line}"

    def handlers(self) -> list[BaseHandler[Update, Context, Any]]:
        return [_handler(c) for c in self]


def _handler(command: Command) -> BaseHandler[Update, Context, Any]:
    async def run(update: Update, context: Context) -> None:
        try:
            result = await command.callback(update, context)
        except Exception:
            count_command(command.name, "error")
            raise
        count_command(command.name, result or "ok")

    return CommandHandler(
        command.name, instrument(f"command:{command.name}", run), filters=command.filters
    )
