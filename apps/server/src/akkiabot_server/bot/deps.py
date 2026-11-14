"""Shared dependencies handed to every callback through ``bot_data``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast

from telegram import Update

from akkiabot_core.config import Settings
from akkiabot_server.bot.registry import CommandRegistry
from akkiabot_server.bot.types import App, Context
from akkiabot_server.services.chats import ChatDirectory
from akkiabot_server.services.ip_lookup import IpLookupService
from akkiabot_server.services.message_log import MessageRecorder
from akkiabot_server.services.start_payload import StartPayload, StartPayloadStore

DEPS_KEY = "akkiabot.deps"

type StartHandler = Callable[[Update, Context, StartPayload], Awaitable[None]]
"""Handles a consumed ``/start`` payload of one ``kind``."""


@dataclass(slots=True)
class BotDeps:
    settings: Settings
    commands: CommandRegistry
    start_payloads: StartPayloadStore
    chats: ChatDirectory
    messages: MessageRecorder
    ip_lookup: IpLookupService
    start_handlers: dict[str, StartHandler] = field(default_factory=dict)

    def on_start(self, kind: str, handler: StartHandler) -> None:
        """Register the handler for ``/start`` payloads of ``kind``."""
        if kind in self.start_handlers:
            raise ValueError(f"start payload 类型 {kind!r} 重复注册")
        self.start_handlers[kind] = handler


def get_deps(context: Context) -> BotDeps:
    return cast(BotDeps, context.bot_data[DEPS_KEY])


def attach_deps(application: App, deps: BotDeps) -> None:
    application.bot_data[DEPS_KEY] = deps
