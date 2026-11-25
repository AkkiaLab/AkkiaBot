"""Polling heartbeat: record every ``getUpdates`` round trip as a metric.

A quiet group sends no updates for hours, so ``updates_total`` alone cannot
tell "nobody is talking" from "the bot stopped receiving updates". The long
poll itself returns every ``timeout`` seconds even when it is empty; recording
its outcome (``telegram_poll_total`` and
``telegram_poll_last_success_timestamp_seconds``) gives the alert a signal
that stays fresh while the bot is healthy. Only polling mode installs this
request, so the timestamp stays 0 (and the alert silent) in webhook mode.

The same wrapper retries a rejected token a few times before giving up. PTB
turns a 401/404 from ``getUpdates`` into ``InvalidToken`` and aborts polling
for good; Telegram has been seen answering that once for a token that was
valid seconds later. A short retry absorbs such a blip without a restart, and
a token that is really revoked still fails within seconds.

:func:`log_polling_error` replaces PTB's polling error callback. PTB retries
every failed ``getUpdates`` forever and logs each failure with a traceback at
ERROR, so a Bad Gateway from Telegram's front end or a dropped connection that
heals seconds later became one Sentry event per retry. Such network errors are
logged as warnings instead; an outage that lasts is still caught by the
heartbeat alert above, and the updates are not lost (Telegram keeps them until
a later ``getUpdates`` confirms them).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, override

from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.request import BaseRequest, HTTPXRequest, RequestData

from akkiabot_core.observability import record_telegram_poll, start_telegram_poll_heartbeat

logger = logging.getLogger(__name__)

GET_UPDATES = "getUpdates"
#: Status codes PTB reports as ``InvalidToken``.
TOKEN_REJECTED = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND})
#: Pauses between ``getUpdates`` attempts while the token is rejected.
TOKEN_REJECTED_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


def log_polling_error(error: TelegramError) -> None:
    """Error callback for :meth:`telegram.ext.Updater.start_polling`.

    PTB keeps retrying after this returns. Transient network failures (5xx
    from Telegram, connection errors) become warnings, which Sentry keeps as
    breadcrumbs only; anything else (``Conflict``, a ``BadRequest``, which PTB
    derives from ``NetworkError``…) is logged like PTB's default callback does.
    """
    if isinstance(error, NetworkError) and not isinstance(error, BadRequest):
        # PTB builds these messages from the HTTP status or the httpx error;
        # they carry no URL, so no token.
        logger.warning(
            "telegram polling failed; retrying",
            extra={"error": type(error).__name__, "detail": error.message},
        )
        return
    logger.error("Exception happened while polling for updates.", exc_info=error)


def default_get_updates_request() -> HTTPXRequest:
    """What :class:`telegram.ext.ApplicationBuilder` builds when nothing is set."""
    return HTTPXRequest(connection_pool_size=1)


class PollHeartbeatRequest(BaseRequest):
    """Delegates to ``inner`` and records the outcome of each ``getUpdates``;
    retries a ``getUpdates`` whose token was rejected (see module docstring)."""

    def __init__(self, inner: BaseRequest, retry_delays: Sequence[float] | None = None) -> None:
        self.inner = inner
        self.retry_delays = tuple(
            TOKEN_REJECTED_RETRY_DELAYS if retry_delays is None else retry_delays
        )

    @property
    @override
    def read_timeout(self) -> float | None:
        return self.inner.read_timeout

    @override
    async def initialize(self) -> None:
        await self.inner.initialize()
        start_telegram_poll_heartbeat()

    @override
    async def shutdown(self) -> None:
        await self.inner.shutdown()

    @override
    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = BaseRequest.DEFAULT_NONE,
        write_timeout: Any = BaseRequest.DEFAULT_NONE,
        connect_timeout: Any = BaseRequest.DEFAULT_NONE,
        pool_timeout: Any = BaseRequest.DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        polled = url.rsplit("/", 1)[-1] == GET_UPDATES
        delays = iter(self.retry_delays if polled else ())
        while True:
            try:
                status, payload = await self.inner.do_request(
                    url,
                    method,
                    request_data,
                    read_timeout=read_timeout,
                    write_timeout=write_timeout,
                    connect_timeout=connect_timeout,
                    pool_timeout=pool_timeout,
                )
            except Exception:  # cancellation at shutdown is not a failed poll
                if polled:
                    record_telegram_poll(ok=False)
                raise
            if polled:
                record_telegram_poll(ok=status == 200)
            delay = next(delays, None) if status in TOKEN_REJECTED else None
            if delay is None:
                return status, payload
            # The URL holds the token: log the status only.
            logger.warning(
                "telegram rejected the bot token while polling; retrying",
                extra={"status_code": status, "retry_in_seconds": delay},
            )
            await asyncio.sleep(delay)
