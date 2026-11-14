"""Prometheus metrics registry and instrumentation helpers.

Business code records metrics through the helpers here (``track_handler``,
``count_update``, ``count_command``…) rather than touching prometheus objects,
so names, labels and buckets stay consistent. Metric names carry the
``akkiabot_`` prefix (see docs/architecture.md §5.5).
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import overload

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    gc_collector,
    generate_latest,
    platform_collector,
    process_collector,
)

from akkiabot_core.observability.context import log_context

logger = logging.getLogger(__name__)

NAMESPACE = "akkiabot"

REGISTRY = CollectorRegistry(auto_describe=True)
process_collector.ProcessCollector(registry=REGISTRY)
platform_collector.PlatformCollector(registry=REGISTRY)
gc_collector.GCCollector(registry=REGISTRY)

UPDATES_TOTAL = Counter(
    "updates_total",
    "Telegram updates received, by update type.",
    ["type"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
HANDLER_DURATION_SECONDS = Histogram(
    "handler_duration_seconds",
    "Time spent in a handler, including failed runs.",
    ["handler"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
HANDLER_ERRORS_TOTAL = Counter(
    "handler_errors_total",
    "Handler invocations that raised an exception.",
    ["handler"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
COMMAND_TOTAL = Counter(
    "command_total",
    "Bot commands processed, by command and result.",
    ["command", "result"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
WHOIS_LOOKUP_TOTAL = Counter(
    "whois_lookup_total",
    "whois lookup outcomes: per provider ok/error, cache hit, all providers exhausted.",
    ["provider", "result"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
MODERATION_ACTIONS_TOTAL = Counter(
    "moderation_actions_total",
    "Moderation actions carried out (ban / warn / mute / delete; delete counts messages).",
    ["action"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
ANTISPAM_VERDICT_TOTAL = Counter(
    "antispam_verdict_total",
    "Spam checks of group messages and /spam reports, by verdict and resulting action.",
    ["verdict", "action"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
ANTISPAM_LLM_DURATION_SECONDS = Histogram(
    "antispam_llm_duration_seconds",
    "Wall time of one spam classification (LLM call, fallback included).",
    namespace=NAMESPACE,
    registry=REGISTRY,
    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 45, 60),
)
ANTISPAM_LLM_ERRORS_TOTAL = Counter(
    "antispam_llm_errors_total",
    "Classifications that failed open (timeout, api_error, parse_error, error).",
    ["error"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
TELEGRAM_API_ERRORS_TOTAL = Counter(
    "telegram_api_errors_total",
    "Failed Bot API calls made by business code, by method (rate limits included).",
    ["method"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)

CAPTCHA_STARTED_TOTAL = Counter(
    "captcha_started_total",
    "Join captcha challenges created (the top of the funnel; outcomes in captcha_total).",
    namespace=NAMESPACE,
    registry=REGISTRY,
)
CAPTCHA_TOTAL = Counter(
    "captcha_total",
    "Join captcha outcomes: passed, timeout, or failed (a rejected verification attempt).",
    ["result"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
CAPTCHA_DURATION_SECONDS = Histogram(
    "captcha_duration_seconds",
    "Time from joining (challenge created) to passing the captcha.",
    namespace=NAMESPACE,
    registry=REGISTRY,
    buckets=(5, 10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600),
)

TELEGRAM_POLL_TOTAL = Counter(
    "telegram_poll_total",
    "getUpdates long polls, by result (ok / error).",
    ["result"],
    namespace=NAMESPACE,
    registry=REGISTRY,
)
TELEGRAM_POLL_LAST_SUCCESS = Gauge(
    "telegram_poll_last_success_timestamp_seconds",
    "Unix time of the last successful getUpdates (set when polling starts, "
    "0 in webhook mode); a stale value means the bot stopped receiving updates.",
    namespace=NAMESPACE,
    registry=REGISTRY,
)


def count_update(update_type: str) -> None:
    UPDATES_TOTAL.labels(type=update_type).inc()


def count_command(command: str, result: str = "ok") -> None:
    COMMAND_TOTAL.labels(command=command, result=result).inc()


def count_whois_lookup(provider: str, result: str) -> None:
    WHOIS_LOOKUP_TOTAL.labels(provider=provider, result=result).inc()


def count_moderation_action(action: str, amount: int = 1) -> None:
    """``action`` is ``ban`` / ``warn`` / ``mute`` / ``delete`` (``amount`` = messages)."""
    if amount > 0:
        MODERATION_ACTIONS_TOTAL.labels(action=action).inc(amount)


def count_antispam_verdict(verdict: str, action: str) -> None:
    """``verdict``: spam / ham / error / skipped; ``action``: what was done about it."""
    ANTISPAM_VERDICT_TOTAL.labels(verdict=verdict, action=action).inc()


def observe_antispam_llm(seconds: float, *, error: str | None = None) -> None:
    ANTISPAM_LLM_DURATION_SECONDS.observe(seconds)
    if error is not None:
        ANTISPAM_LLM_ERRORS_TOTAL.labels(error=error).inc()


def count_telegram_api_error(method: str) -> None:
    TELEGRAM_API_ERRORS_TOTAL.labels(method=method).inc()


def count_captcha(result: str) -> None:
    """``result`` is ``passed`` / ``timeout`` / ``failed``."""
    CAPTCHA_TOTAL.labels(result=result).inc()


def count_captcha_started() -> None:
    CAPTCHA_STARTED_TOTAL.inc()


def start_telegram_poll_heartbeat() -> None:
    """Polling starts: begin the staleness clock now, so a bot that never
    manages a successful poll still trips the alert."""
    TELEGRAM_POLL_LAST_SUCCESS.set_to_current_time()


def record_telegram_poll(*, ok: bool) -> None:
    """One getUpdates round trip finished; ``ok`` = Telegram answered successfully."""
    TELEGRAM_POLL_TOTAL.labels(result="ok" if ok else "error").inc()
    if ok:
        TELEGRAM_POLL_LAST_SUCCESS.set_to_current_time()


def observe_captcha_duration(seconds: float) -> None:
    CAPTCHA_DURATION_SECONDS.observe(max(seconds, 0.0))


type _AsyncFn[**P, R] = Callable[P, Awaitable[R]]


@overload
def track_handler[**P, R](name: str) -> Callable[[_AsyncFn[P, R]], _AsyncFn[P, R]]: ...
@overload
def track_handler[**P, R](name: _AsyncFn[P, R]) -> _AsyncFn[P, R]: ...
def track_handler[**P, R](
    name: str | _AsyncFn[P, R],
) -> Callable[[_AsyncFn[P, R]], _AsyncFn[P, R]] | _AsyncFn[P, R]:
    """Decorate an async handler to record duration and errors.

    Usable as ``@track_handler`` (label = function ``__qualname__``) or
    ``@track_handler("name")``. The handler name is also bound to the log
    context. Exceptions are counted, logged and re-raised unchanged.
    """

    def decorate(fn: _AsyncFn[P, R], label: str) -> _AsyncFn[P, R]:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError("track_handler 只能装饰 async 函数")

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            start = time.perf_counter()
            with log_context(handler=label):
                try:
                    return await fn(*args, **kwargs)
                except Exception:
                    HANDLER_ERRORS_TOTAL.labels(handler=label).inc()
                    logger.exception("handler failed")
                    raise
                finally:
                    HANDLER_DURATION_SECONDS.labels(handler=label).observe(
                        time.perf_counter() - start
                    )

        return wrapper

    if isinstance(name, str):
        label = name
        return lambda fn: decorate(fn, label)
    return decorate(name, name.__qualname__)


def render_metrics() -> tuple[bytes, str]:
    """Exposition payload and content type for a ``/metrics`` endpoint."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
