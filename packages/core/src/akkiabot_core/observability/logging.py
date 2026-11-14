"""Structured logging: JSON lines on stdout with context fields.

This module deliberately does **not** redact anything: log content (messages,
``extra`` fields, exceptions) is emitted verbatim so it stays useful for
debugging. Keeping secrets out of logs is the caller's job — log
``SecretStr`` / ``DatabaseSettings.safe_url`` / ``Settings.redacted()`` rather
than raw values, and rewrite known secrets before logging them.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal, override

from akkiabot_core.observability.context import get_log_context

# Attributes every LogRecord has; anything else came from `extra=`.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``message``,
    the bound log context, ``extra=`` fields and ``exc_info`` if any."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(get_log_context())
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line format for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    @override
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ctx = " ".join(f"{k}={v}" for k, v in get_log_context().items())
        return f"{line} [{ctx}]" if ctx else line


_HANDLER_NAME = "akkiabot"


def configure_logging(
    level: str = "INFO",
    *,
    fmt: Literal["json", "console"] = "json",
    stream: Any = None,
) -> logging.Handler:
    """Install a single stdout handler on the root logger (idempotent)."""
    formatter = JsonFormatter() if fmt == "json" else ConsoleFormatter()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
