"""FastAPI application: health probes and the Telegram webhook endpoint."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from telegram import Update

from akkiabot_server import __version__
from akkiabot_server.runtime import WEBHOOK_PATH, Runtime

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - header name


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="AkkiaBot", version=__version__, lifespan=lifespan)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness: the process is up and serving HTTP."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Readiness: database and cache are reachable."""
        checks = await runtime.readiness()
        ok = all(checks.values())
        return JSONResponse(
            {"status": "ok" if ok else "unavailable", "checks": checks},
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    telegram = runtime.settings.telegram
    if telegram.mode == "webhook" and telegram.webhook_secret is not None:
        expected = telegram.webhook_secret.get_secret_value()

        @app.post(WEBHOOK_PATH, include_in_schema=False)
        async def telegram_webhook(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
            given = request.headers.get(SECRET_HEADER, "")
            if not hmac.compare_digest(given.encode(), expected.encode()):
                return Response(status_code=status.HTTP_403_FORBIDDEN)
            try:
                data: Any = await request.json()
                update = Update.de_json(data, runtime.application.bot)
            except (ValueError, TypeError, KeyError):
                logger.warning("malformed webhook payload")
                return Response(status_code=status.HTTP_400_BAD_REQUEST)
            await runtime.application.update_queue.put(update)
            return Response(status_code=status.HTTP_200_OK)

    return app
