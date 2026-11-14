"""Observability baseline: structured logs, Sentry and Prometheus metrics."""

from __future__ import annotations

from akkiabot_core.config import ObservabilitySettings
from akkiabot_core.observability.context import get_log_context, log_context, new_trace_id
from akkiabot_core.observability.logging import configure_logging
from akkiabot_core.observability.metrics import (
    REGISTRY,
    count_command,
    count_update,
    render_metrics,
    track_handler,
)
from akkiabot_core.observability.sentry import init_sentry


def setup_observability(
    settings: ObservabilitySettings,
    *,
    release: str | None = None,
) -> None:
    """Configure logging and (optionally) Sentry at process startup."""
    configure_logging(settings.log_level, fmt=settings.log_format)
    init_sentry(settings, release=release)


__all__ = [
    "REGISTRY",
    "configure_logging",
    "count_command",
    "count_update",
    "get_log_context",
    "init_sentry",
    "log_context",
    "new_trace_id",
    "render_metrics",
    "setup_observability",
    "track_handler",
]
