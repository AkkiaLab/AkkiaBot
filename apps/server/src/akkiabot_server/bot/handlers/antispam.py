"""Group messages → :class:`~akkiabot_server.services.antispam_guard.AntiSpamGuard`.

The check runs as a background task (the LLM call can take seconds and PTB
handles updates one by one), so other handlers are never blocked.
"""

from __future__ import annotations

import contextlib

from telegram import Update

from akkiabot_core.observability import track_handler
from akkiabot_server.bot.deps import BotDeps, get_deps
from akkiabot_server.bot.moderation_api import PtbAntiSpamApi
from akkiabot_server.bot.types import Context
from akkiabot_server.services.antispam_guard import AntiSpamApi, GroupMessage


@track_handler("antispam_guard")
async def _guard(deps: BotDeps, api: AntiSpamApi, message: GroupMessage) -> None:
    await deps.antispam.check(api, message)


async def _run(deps: BotDeps, api: AntiSpamApi, message: GroupMessage) -> None:
    # Failures are counted and logged by track_handler.
    with contextlib.suppress(Exception):
        await _guard(deps, api, message)


async def check_group_message(update: Update, context: Context) -> None:
    """Text and media captions of human members (not channels / anonymous admins / bots)."""
    message = update.message
    if message is None or message.sender_chat is not None:
        return
    sender = message.from_user
    if sender is None or sender.is_bot:
        return
    text = message.text or message.caption or ""
    if not text.strip():
        return
    deps = get_deps(context)
    # Cheap (cached) pre-check so disabled groups do not spawn a task per message.
    if not (await deps.chat_settings.get(message.chat.id)).antispam:
        return
    candidate = GroupMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,
        sender_id=sender.id,
        sender_name=sender.full_name,
        text=text,
    )
    deps.background.spawn(
        _run(deps, PtbAntiSpamApi(context.bot), candidate),
        name=f"antispam:check:{message.chat.id}:{message.message_id}",
    )
