"""Sentry initialisation driven purely by configuration."""

from __future__ import annotations

import logging

from akkiabot_core.config import ObservabilitySettings

logger = logging.getLogger(__name__)


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
    )
    logger.info(
        "sentry enabled",
        extra={
            "sentry_environment": settings.sentry_environment,
            "sentry_traces_sample_rate": settings.sentry_traces_sample_rate,
        },
    )
    return True
