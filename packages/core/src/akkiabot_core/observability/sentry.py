"""Sentry initialisation driven purely by configuration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from akkiabot_core.config import ObservabilitySettings

if TYPE_CHECKING:
    from sentry_sdk.integrations import Integration

logger = logging.getLogger(__name__)


def disabled_integrations() -> list[type[Integration]]:
    """Auto-enabling integrations that must stay off.

    ``OpenAIIntegration`` is switched on automatically whenever ``openai`` is
    installed. It reports every SDK-level error as *unhandled* before our
    callers' ``except`` runs (the anti-spam classifier fails open on purpose and
    already logs a warning and counts ``antispam_llm_errors_total``), and its
    ``chat <model>`` spans leak into the transaction name of unrelated events.
    LLM tracing is Langfuse's job, so the integration adds noise only.
    """
    from sentry_sdk.integrations import DidNotEnable

    try:
        from sentry_sdk.integrations.openai import OpenAIIntegration
    except (DidNotEnable, ImportError):  # openai not installed: nothing auto-enables
        return []
    return [OpenAIIntegration]


def init_sentry(settings: ObservabilitySettings, *, release: str | None = None) -> bool:
    """Initialise the Sentry SDK if a DSN is configured.

    Returns ``False`` (and does not import or initialise the SDK at all) when
    no DSN is set, so nothing is sent anywhere.
    """
    if settings.sentry_dsn is None:
        logger.info("sentry disabled: no DSN configured")
        return False

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        release=release,
        # No PII options: the SDK default sends none (``send_default_pii`` is
        # deprecated in favour of ``data_collection`` and warns when passed).
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        disabled_integrations=disabled_integrations(),
    )
    logger.info(
        "sentry enabled",
        extra={
            "sentry_environment": settings.sentry_environment,
            "sentry_traces_sample_rate": settings.sentry_traces_sample_rate,
        },
    )
    return True
