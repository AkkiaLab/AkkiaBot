"""Sentry initialisation driven purely by configuration."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

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


REDACTED = "***"

#: Sentry protocol fields kept verbatim wherever they appear: identifiers,
#: timestamps and SDK metadata. They never carry application data, and
#: rewriting one breaks the event (a changed ``event_id`` makes the SDK drop
#: it) when a short secret happens to match part of it.
PROTOCOL_FIELDS = frozenset(
    {
        "event_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "segment_id",
        "profile_id",
        "replay_id",
        "timestamp",
        "start_timestamp",
        "type",
        "level",
        "platform",
        "release",
        "dist",
        "environment",
        "server_name",
        "sdk",
        "debug_meta",
    }
)


def secret_scrubber(secrets: Iterable[str]) -> Callable[[Any], Any]:
    """Return a function that copies a Sentry payload with ``secrets`` masked.

    Third-party errors can embed secrets in their messages, e.g. PTB's
    ``InvalidToken`` quotes the bot token. Only values are masked: keys and
    :data:`PROTOCOL_FIELDS` keep the event's structure intact.
    """
    # Longest first, so a secret containing another is masked whole.
    masked = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            for secret in masked:
                value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {
                k: v if k in PROTOCOL_FIELDS else scrub(v)
                for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
            }
        if isinstance(value, (list, tuple)):
            return [scrub(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return value

    return scrub


def init_sentry(
    settings: ObservabilitySettings,
    *,
    release: str | None = None,
    secrets: Iterable[str] = (),
) -> bool:
    """Initialise the Sentry SDK if a DSN is configured.

    Returns ``False`` (and does not import or initialise the SDK at all) when
    no DSN is set, so nothing is sent anywhere. ``secrets`` (the configured
    secret values) are masked anywhere in events and breadcrumbs.
    """
    if settings.sentry_dsn is None:
        logger.info("sentry disabled: no DSN configured")
        return False

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    scrub = secret_scrubber(secrets)

    def scrub_payload(payload: Any, _hint: Any) -> Any:
        return scrub(payload)

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        release=release,
        # No PII options: the SDK default sends none (``send_default_pii`` is
        # deprecated in favour of ``data_collection`` and warns when passed).
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        disabled_integrations=disabled_integrations(),
        # Frame locals hold request URLs with the bot token (``/bot<token>/…``)
        # and user payloads; tracebacks are enough to debug.
        include_local_variables=False,
        before_send=scrub_payload,
        before_send_transaction=scrub_payload,
        before_breadcrumb=scrub_payload,
    )
    logger.info(
        "sentry enabled",
        extra={
            "sentry_environment": settings.sentry_environment,
            "sentry_traces_sample_rate": settings.sentry_traces_sample_rate,
        },
    )
    return True
