"""Uniform instrumentation for every PTB callback.

Each handler is wrapped so that it

* runs with ``update_id`` / ``chat_id`` / ``user_id`` / ``trace_id`` bound to
  the log context;
* records ``handler_duration_seconds`` and ``handler_errors_total`` through
  :func:`akkiabot_core.observability.track_handler`, which also logs the
  exception at ERROR level — the Sentry logging integration turns that record
  into a Sentry event (when a DSN is configured);
* never lets the exception reach PTB, so the same failure is not reported a
  second time by the application error handler.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from telegram import Update

from akkiabot_core.observability import count_update, log_context, new_trace_id, track_handler
from akkiabot_server.bot.types import Context

logger = logging.getLogger(__name__)

type Callback = Callable[[Update, Context], Awaitable[Any]]
type WrappedCallback = Callable[[Update, Context], Coroutine[Any, Any, None]]


def update_fields(update: object) -> dict[str, Any]:
    """Log context fields describing ``update``."""
    if not isinstance(update, Update):
        return {}
    chat, user = update.effective_chat, update.effective_user
    return {
        "update_id": update.update_id,
        "chat_id": chat.id if chat else None,
        "user_id": user.id if user else None,
    }


def update_type(update: Update) -> str:
    """The payload field of the update (``message``, ``callback_query``…)."""
    return next((key for key in update.to_dict() if key != "update_id"), "unknown")


def instrument(name: str, callback: Callback) -> WrappedCallback:
    """Wrap ``callback`` with log context, metrics and error reporting."""
    tracked = track_handler(name)(callback)

    @functools.wraps(callback)
    async def wrapper(update: Update, context: Context) -> None:
        # Exceptions were already counted and logged (→ Sentry) by track_handler.
        with (
            log_context(trace_id=new_trace_id(), **update_fields(update)),
            contextlib.suppress(Exception),
        ):
            await tracked(update, context)

    return wrapper


async def count_incoming_update(update: Update, _context: Context) -> None:
    """Group ``-1000`` hook: count every update once, by type."""
    count_update(update_type(update))


async def on_error(update: object, context: Context) -> None:
    """Application error handler: errors outside wrapped handlers (polling, jobs…)."""
    with log_context(**update_fields(update)):
        logger.error("unhandled telegram error", exc_info=context.error)
