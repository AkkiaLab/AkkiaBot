"""Observability baseline: structured logs, Sentry and Prometheus metrics."""

from __future__ import annotations

from akkiabot_core.config import ObservabilitySettings
from akkiabot_core.observability.context import get_log_context, log_context, new_trace_id
from akkiabot_core.observability.logging import configure_logging
from akkiabot_core.observability.metrics import (
    REGISTRY,
    count_antispam_verdict,
    count_captcha,
    count_command,
    count_moderation_action,
    count_telegram_api_error,
    count_update,
    count_whois_lookup,
    observe_antispam_llm,
    observe_captcha_duration,
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
    "count_antispam_verdict",
    "count_captcha",
    "count_command",
    "count_moderation_action",
    "count_telegram_api_error",
    "count_update",
    "count_whois_lookup",
    "get_log_context",
    "init_sentry",
    "log_context",
    "new_trace_id",
    "observe_antispam_llm",
    "observe_captcha_duration",
    "render_metrics",
    "setup_observability",
    "track_handler",
]
