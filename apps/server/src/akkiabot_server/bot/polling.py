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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, override

from telegram.request import BaseRequest, HTTPXRequest, RequestData

from akkiabot_core.observability import record_telegram_poll, start_telegram_poll_heartbeat

logger = logging.getLogger(__name__)

GET_UPDATES = "getUpdates"
#: Status codes PTB reports as ``InvalidToken``.
TOKEN_REJECTED = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.NOT_FOUND})
#: Pauses between ``getUpdates`` attempts while the token is rejected.
TOKEN_REJECTED_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


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
