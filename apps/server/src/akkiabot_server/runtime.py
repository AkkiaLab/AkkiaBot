"""Process-wide runtime: database, cache, bot and metrics listener.

:class:`Runtime` owns every long-lived resource. The FastAPI lifespan calls
:meth:`Runtime.start` / :meth:`Runtime.stop`, so PTB and uvicorn share one
event loop and shut down together.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from wsgiref.simple_server import WSGIServer

from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram.request import BaseRequest

from akkiabot_core.cache import Cache, create_cache
from akkiabot_core.config import Settings
from akkiabot_core.db import create_engine, create_session_factory, ping
from akkiabot_core.observability import REGISTRY
from akkiabot_server.bot.application import (
    ALLOWED_UPDATES,
    build_application,
    sync_commands,
)
from akkiabot_server.bot.commands import COMMANDS
from akkiabot_server.bot.deps import BotDeps
from akkiabot_server.bot.registry import CommandRegistry
from akkiabot_server.bot.types import App
from akkiabot_server.services.chats import ChatDirectory
from akkiabot_server.services.message_log import MessageRecorder
from akkiabot_server.services.start_payload import StartPayloadStore

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"

# Bot API URLs embed the bot token (``/bot<token>/getMe``), and some loggers
# print them verbatim: httpx logs every request line at INFO, and PTB's Bot
# logs "Set Bot API URL: …" at DEBUG when constructed. Cap those loggers
# whatever the root level is (callers keep known secrets out of the logs; the
# logging module does no redaction).
_TOKEN_LEAKING_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "telegram.Bot": logging.INFO,
    "telegram.ext.ExtBot": logging.INFO,
}


def silence_token_leaking_logs() -> None:
    for name, level in _TOKEN_LEAKING_LOGGERS.items():
        logging.getLogger(name).setLevel(level)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    cache: Cache
    deps: BotDeps
    application: App
    _metrics: tuple[WSGIServer, threading.Thread] | None = field(default=None, init=False)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        cache: Cache | None = None,
        request: BaseRequest | None = None,
        get_updates_request: BaseRequest | None = None,
    ) -> Runtime:
        silence_token_leaking_logs()  # before the bot is constructed or sends anything
        engine = create_engine(settings.db.url, echo=settings.db.echo)
        session_factory = create_session_factory(engine)
        if cache is None:
            redis_url = settings.redis.url
            cache = create_cache(redis_url.get_secret_value() if redis_url else None)
        chats = ChatDirectory(session_factory)
        deps = BotDeps(
            settings=settings,
            commands=CommandRegistry(COMMANDS),
            start_payloads=StartPayloadStore(
                cache, default_ttl=settings.telegram.start_payload_ttl
            ),
            chats=chats,
            messages=MessageRecorder(session_factory, chats),
        )
        application = build_application(
            deps,
            webhook=settings.telegram.mode == "webhook",
            request=request,
            get_updates_request=get_updates_request,
        )
        return cls(settings, engine, session_factory, cache, deps, application)

    @property
    def webhook_url(self) -> str | None:
        base = self.settings.telegram.webhook_url
        return f"{str(base).rstrip('/')}{WEBHOOK_PATH}" if base else None

    @property
    def metrics_port(self) -> int | None:
        return self._metrics[0].server_port if self._metrics else None

    async def start(self) -> None:
        try:
            await self._start()
        except Exception as exc:
            await self.stop()
            # PTB puts the bot token into some errors (InvalidToken); rewrite it
            # before anything logs the exception (uvicorn logs startup errors).
            token = self.settings.telegram.bot_token.get_secret_value()
            message = f"{type(exc).__name__}: {exc}".replace(token, "***")
            logger.error("bot startup failed: %s", message)  # no traceback: it may hold the token
            raise RuntimeError(f"bot startup failed: {message}") from None
        except BaseException:
            await self.stop()
            raise

    async def _start(self) -> None:
        self._start_metrics()
        telegram = self.settings.telegram
        await self.application.initialize()
        await sync_commands(self.application, self.deps.commands)
        await self.application.start()
        if telegram.mode == "webhook":
            assert telegram.webhook_secret is not None  # noqa: S101 - enforced by settings
            await self.application.bot.set_webhook(
                url=self.webhook_url or "",
                secret_token=telegram.webhook_secret.get_secret_value(),
                allowed_updates=ALLOWED_UPDATES,
            )
        else:
            updater = self.application.updater
            assert updater is not None  # noqa: S101 - polling mode keeps the updater
            await updater.start_polling(allowed_updates=ALLOWED_UPDATES)
        logger.info(
            "bot started",
            extra={"mode": telegram.mode, "bot_username": self.application.bot.username},
        )

    async def stop(self) -> None:
        updater = self.application.updater
        if updater is not None and updater.running:
            await updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
        await self.cache.aclose()
        await self.engine.dispose()
        self._stop_metrics()
        logger.info("bot stopped")

    async def readiness(self) -> dict[str, bool]:
        """Per-dependency readiness for ``/readyz``."""
        checks: dict[str, bool] = {}
        for name, probe in (("db", lambda: ping(self.engine)), ("cache", self.cache.ping)):
            try:
                checks[name] = bool(await probe())
            except Exception:
                logger.warning("readiness check failed", extra={"check": name}, exc_info=True)
                checks[name] = False
        return checks

    def _start_metrics(self) -> None:
        obs = self.settings.observability
        if not obs.metrics_enabled or self._metrics is not None:
            return
        self._metrics = start_http_server(
            obs.metrics_port, addr=obs.metrics_host, registry=REGISTRY
        )
        logger.info(
            "metrics listener started",
            extra={"metrics_host": obs.metrics_host, "metrics_port": self.metrics_port},
        )

    def _stop_metrics(self) -> None:
        if self._metrics is None:
            return
        server, thread = self._metrics
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        self._metrics = None
