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

type Spec = bool | dict[str, Spec] | list[Spec]
"""Where a payload keeps Sentry protocol values verbatim: ``True`` keeps the
whole value, a dict maps the keys of a dict, a one-item list applies to every
item of a list. Anything the spec does not name is scrubbed."""

_TRACE_IDS: Spec = {"trace_id": True, "span_id": True, "parent_span_id": True}

#: Protocol values of a breadcrumb (``before_breadcrumb`` and ``breadcrumbs``).
BREADCRUMB_PROTOCOL: Spec = {"timestamp": True, "type": True, "level": True}

#: Protocol values of an error or transaction event: identifiers, timestamps
#: and SDK metadata, only at the places the event schema puts them. They carry
#: no application data, and rewriting one breaks the event (the SDK drops an
#: event whose ``event_id`` is gone) when a short secret matches part of it.
#: Application data (``extra``, tags, messages…) is scrubbed even where it
#: reuses one of these key names.
EVENT_PROTOCOL: Spec = {
    "event_id": True,
    "timestamp": True,
    "start_timestamp": True,
    "type": True,
    "level": True,
    "platform": True,
    "release": True,
    "dist": True,
    "environment": True,
    "server_name": True,
    "sdk": True,
    "debug_meta": True,
    "contexts": {
        "trace": _TRACE_IDS,
        "profile": {"profile_id": True},
        "replay": {"replay_id": True},
    },
    "spans": [
        {
            **_TRACE_IDS,
            "segment_id": True,
            "start_timestamp": True,
            "timestamp": True,
        }
    ],
    "breadcrumbs": {"values": [BREADCRUMB_PROTOCOL]},
}


def secret_scrubber(secrets: Iterable[str], keep: Spec = False) -> Callable[[Any], Any]:
    """Return a function that copies a Sentry payload with ``secrets`` masked.

    Third-party errors can embed secrets in their messages, e.g. PTB's
    ``InvalidToken`` quotes the bot token. Only values are masked (keys stay
    intact), except the protocol values ``keep`` names.
    """
    # Longest first, so a secret containing another is masked whole.
    masked = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(value: Any, spec: Spec) -> Any:
        if spec is True:
            return value
        if isinstance(value, str):
            for secret in masked:
                value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            keys: dict[str, Spec] = spec if isinstance(spec, dict) else {}
            return {
                k: scrub(v, keys.get(k, False))
                for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
            }
        if isinstance(value, (list, tuple)):
            item: Spec = spec[0] if isinstance(spec, list) and spec else False
            return [scrub(v, item) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return value

    return lambda payload: scrub(payload, keep)


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

    scrub_event = secret_scrubber(secrets, EVENT_PROTOCOL)
    scrub_breadcrumb = secret_scrubber(secrets, BREADCRUMB_PROTOCOL)

    def scrub_event_hook(event: Any, _hint: Any) -> Any:
        return scrub_event(event)

    def scrub_breadcrumb_hook(crumb: Any, _hint: Any) -> Any:
        return scrub_breadcrumb(crumb)

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
        before_send=scrub_event_hook,
        before_send_transaction=scrub_event_hook,
        before_breadcrumb=scrub_breadcrumb_hook,
    )
    logger.info(
        "sentry enabled",
        extra={
            "sentry_environment": settings.sentry_environment,
            "sentry_traces_sample_rate": settings.sentry_traces_sample_rate,
        },
    )
    return True
