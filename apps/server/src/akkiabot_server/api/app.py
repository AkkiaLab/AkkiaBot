"""FastAPI application: health probes, captcha API and the Telegram webhook."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from akkiabot_server import __version__
from akkiabot_server.api import captcha, health
from akkiabot_server.runtime import WEBHOOK_PATH, Runtime

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - header name


def _base_app(**kwargs: Any) -> FastAPI:
    """Routes shared by the real app and the OpenAPI export."""
    app = FastAPI(title="AkkiaBot", version=__version__, **kwargs)
    app.include_router(health.router)
    app.include_router(captcha.router)
    return app


def openapi_document() -> dict[str, Any]:
    """The public API contract (input of ``just gen-api``); needs no settings."""
    return _base_app().openapi()


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = _base_app(lifespan=lifespan)
    app.state.runtime = runtime

    origins = runtime.settings.cors.allow_origins
    if origins:
        # Only the configured Mini App origins (production: https://bot.akkia.moe).
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["POST"],
            allow_headers=["Content-Type"],
            allow_credentials=False,
            max_age=600,
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
