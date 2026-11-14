"""Structured logging: JSON lines on stdout with context fields and redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, override

from akkiabot_core.observability.context import get_log_context

# Attributes every LogRecord has; anything else came from `extra=`.
_RESERVED = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys() | {"message", "asctime"}
)

# Telegram bot tokens leak through library URLs such as
# https://api.telegram.org/bot<token>/getUpdates, so mask them by shape too.
_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{30,}")
_MASK = "***"


class Redactor:
    """Replaces known secret values and bot-token-shaped strings with ``***``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        self.add(secrets)

    def add(self, secrets: Iterable[str]) -> None:
        # Very short values would mask random substrings; ignore them.
        new = {s for s in secrets if len(s) >= 4} | set(self._secrets)
        self._secrets = sorted(new, key=len, reverse=True)

    def redact_value(self, value: Any) -> Any:
        """Recursively redact strings in ``value``; non-JSON objects become ``str``."""
        if isinstance(value, str):
            return self(value)
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, Mapping):
            return {
                self(str(k)): self.redact_value(v)
                for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
            }
        if isinstance(value, list | tuple | set | frozenset):
            return [self.redact_value(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return self(str(value))

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, _MASK)
        return _BOT_TOKEN_RE.sub(_MASK, text)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``message``,
    the bound log context, ``extra=`` fields and ``exc_info`` if any."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or Redactor()

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
        # Redact the raw values *before* serialising: JSON escaping would turn
        # a secret containing `"`, `\\` or a newline into a different string.
        return json.dumps(self.redactor.redact_value(payload), ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single-line format for local development."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self.redactor = redactor or Redactor()

    @override
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        ctx = " ".join(f"{k}={v}" for k, v in get_log_context().items())
        return self.redactor(f"{line} [{ctx}]" if ctx else line)


_HANDLER_NAME = "akkiabot"


def configure_logging(
    level: str = "INFO",
    *,
    fmt: Literal["json", "console"] = "json",
    secrets: Iterable[str] = (),
    stream: Any = None,
) -> logging.Handler:
    """Install a single stdout handler on the root logger (idempotent)."""
    redactor = Redactor(secrets)
    formatter = JsonFormatter(redactor) if fmt == "json" else ConsoleFormatter(redactor)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() == _HANDLER_NAME:
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs every request URL at INFO, which includes the bot token.
    logging.getLogger("httpx").setLevel(max(logging.WARNING, root.level))
    return handler
