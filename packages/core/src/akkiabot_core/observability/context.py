"""Per-task log context carried in a :mod:`contextvars` variable.

Values bound here (``chat_id``, ``user_id``, ``update_id``, ``trace_id``,
``handler``…) are attached to every log record emitted in the same asyncio
task, and inherited by tasks spawned from it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_log_context: ContextVar[Mapping[str, Any]] = ContextVar("akkiabot_log_context", default=_EMPTY)


def get_log_context() -> Mapping[str, Any]:
    """Return the fields bound in the current context (read-only)."""
    return _log_context.get()


@contextmanager
def log_context(**fields: Any) -> Iterator[Mapping[str, Any]]:
    """Bind ``fields`` for the duration of the ``with`` block.

    ``None`` values are ignored, so callers can pass optional ids directly.
    Nested blocks add to (and may override) the outer fields; the outer
    context is restored on exit.
    """
    merged = {**_log_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _log_context.set(MappingProxyType(merged))
    try:
        yield _log_context.get()
    finally:
        _log_context.reset(token)


def new_trace_id() -> str:
    """A random 32-hex-digit id (same shape as W3C / Sentry trace ids)."""
    return uuid.uuid4().hex
