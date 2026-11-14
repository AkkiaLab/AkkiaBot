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


def count_update(update_type: str) -> None:
    UPDATES_TOTAL.labels(type=update_type).inc()


def count_command(command: str, result: str = "ok") -> None:
    COMMAND_TOTAL.labels(command=command, result=result).inc()


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
