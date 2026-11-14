"""Shared dependencies handed to every callback through ``bot_data``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast

from telegram import Update

from akkiabot_core.config import Settings
from akkiabot_server.bot.registry import CommandRegistry
from akkiabot_server.bot.types import App, Context
from akkiabot_server.services.antispam_guard import AntiSpamGuard
from akkiabot_server.services.background import BackgroundTasks
from akkiabot_server.services.captcha import CaptchaService
from akkiabot_server.services.chat_settings import ChatSettingsService
from akkiabot_server.services.chats import ChatDirectory
from akkiabot_server.services.ip_lookup import IpLookupService
from akkiabot_server.services.message_log import MessageRecorder
from akkiabot_server.services.moderation import ModerationService
from akkiabot_server.services.rate_limit import RateLimiter
from akkiabot_server.services.start_payload import StartPayload, StartPayloadStore

DEPS_KEY = "akkiabot.deps"

type StartHandler = Callable[[Update, Context, StartPayload], Awaitable[None]]
"""Handles a ``/start`` payload of one ``kind``."""


@dataclass(frozen=True, slots=True)
class StartRoute:
    handler: StartHandler
    reusable: bool = False
    """Keep the payload after use (until its TTL) instead of consuming it.

    For links whose handler re-validates the user itself, so that a stranger
    tapping someone else's link cannot burn it."""


@dataclass(slots=True)
class BotDeps:
    settings: Settings
    commands: CommandRegistry
    start_payloads: StartPayloadStore
    chats: ChatDirectory
    messages: MessageRecorder
    ip_lookup: IpLookupService
    chat_settings: ChatSettingsService
    moderation: ModerationService
    captcha: CaptchaService
    background: BackgroundTasks
    antispam: AntiSpamGuard
    spam_reports: RateLimiter
    """Per-user limit of ``/spam`` reports by ordinary members (LLM cost)."""
    start_handlers: dict[str, StartRoute] = field(default_factory=dict)

    def on_start(self, kind: str, handler: StartHandler, *, reusable: bool = False) -> None:
        """Register the handler for ``/start`` payloads of ``kind``.

        Payloads are one-time by default; see :attr:`StartRoute.reusable`.
        """
        if kind in self.start_handlers:
            raise ValueError(f"start payload 类型 {kind!r} 重复注册")
        self.start_handlers[kind] = StartRoute(handler, reusable)


def get_deps(context: Context) -> BotDeps:
    return cast(BotDeps, context.bot_data[DEPS_KEY])


def attach_deps(application: App, deps: BotDeps) -> None:
    application.bot_data[DEPS_KEY] = deps
