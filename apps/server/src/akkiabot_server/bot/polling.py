"""Polling heartbeat: record every ``getUpdates`` round trip as a metric.

A quiet group sends no updates for hours, so ``updates_total`` alone cannot
tell "nobody is talking" from "the bot stopped receiving updates". The long
poll itself returns every ``timeout`` seconds even when it is empty; recording
its outcome (``telegram_poll_total`` and
``telegram_poll_last_success_timestamp_seconds``) gives the alert a signal
that stays fresh while the bot is healthy. Only polling mode installs this
request, so the timestamp stays 0 (and the alert silent) in webhook mode.
"""

from __future__ import annotations

from typing import Any, override

from telegram.request import BaseRequest, HTTPXRequest, RequestData

from akkiabot_core.observability import record_telegram_poll, start_telegram_poll_heartbeat

GET_UPDATES = "getUpdates"


def default_get_updates_request() -> HTTPXRequest:
    """What :class:`telegram.ext.ApplicationBuilder` builds when nothing is set."""
    return HTTPXRequest(connection_pool_size=1)


class PollHeartbeatRequest(BaseRequest):
    """Delegates to ``inner`` and records the outcome of each ``getUpdates``."""

    def __init__(self, inner: BaseRequest) -> None:
        self.inner = inner

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
        return status, payload
