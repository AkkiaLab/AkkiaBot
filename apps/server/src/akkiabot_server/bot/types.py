"""Type aliases for the python-telegram-bot objects used across the bot layer."""

from __future__ import annotations

from typing import Any

from telegram.ext import Application, CallbackContext, ExtBot, JobQueue

type Context = CallbackContext[ExtBot[None], dict[Any, Any], dict[Any, Any], dict[Any, Any]]
type App = Application[
    ExtBot[None], Context, dict[Any, Any], dict[Any, Any], dict[Any, Any], JobQueue[Context]
]
