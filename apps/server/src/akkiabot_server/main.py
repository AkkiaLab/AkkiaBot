"""Command-line entry point of the AkkiaBot server.

* ``akkiabot`` / ``akkiabot --version`` — print the version (container smoke test).
* ``akkiabot migrate [REVISION]`` — apply Alembic migrations (default ``head``)
  to the database configured by ``DB_URL``. Run it before starting the app.
* ``akkiabot serve`` — run the bot and the HTTP API in one event loop.
* ``akkiabot openapi [--output FILE]`` — write the OpenAPI document (used by
  ``just gen-api``); needs no configuration.
"""

import argparse
import json
import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from akkiabot_core.config import (
    load_database_settings,
    load_observability_settings,
    load_settings,
)
from akkiabot_core.db.migrate import upgrade
from akkiabot_core.observability import configure_logging, setup_observability
from akkiabot_server import __version__, release_name

logger = logging.getLogger("akkiabot_server")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akkiabot", description="AkkiaBot server")
    parser.add_argument("--version", action="version", version=f"akkiabot {__version__}")
    sub = parser.add_subparsers(dest="command")
    migrate = sub.add_parser("migrate", help="执行数据库迁移（读取 DB_URL）")
    migrate.add_argument("revision", nargs="?", default="head", help="目标版本，默认 head")
    sub.add_parser("serve", help="启动机器人与 HTTP API")
    openapi = sub.add_parser("openapi", help="输出 OpenAPI 文档（just gen-api 使用）")
    openapi.add_argument("--output", "-o", type=Path, help="写入文件，默认输出到 stdout")
    return parser


def _openapi(output: Path | None) -> int:
    from akkiabot_server.api import openapi_document

    text = json.dumps(openapi_document(), ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(text, end="")
    else:
        output.write_text(text, encoding="utf-8")
    return 0


def _migrate(revision: str) -> int:
    obs = load_observability_settings()
    try:
        db = load_database_settings()
    except ValidationError as exc:
        configure_logging(obs.log_level, fmt=obs.log_format)
        logger.error("invalid database configuration: %s", exc)
        return 2
    configure_logging(obs.log_level, fmt=obs.log_format)
    logger.info("running migrations", extra={"db_url": db.safe_url, "revision": revision})
    upgrade(db.url, revision)
    logger.info("migrations complete", extra={"revision": revision})
    return 0


def _serve() -> int:
    obs = load_observability_settings()
    configure_logging(obs.log_level, fmt=obs.log_format)
    try:
        settings = load_settings()
    except ValidationError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2

    import uvicorn

    from akkiabot_server.api import create_app
    from akkiabot_server.runtime import Runtime

    release = release_name()
    setup_observability(settings.observability, release=release)
    logger.info(
        "starting akkiabot",
        extra={"version": __version__, "release": release, "config": settings.redacted()},
    )
    runtime = Runtime.build(settings)
    app = create_app(runtime)
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,  # keep our structured logging
        access_log=False,
        lifespan="on",
        server_header=False,
    )
    server = uvicorn.Server(config)
    # A graceful stop, like SIGTERM, but without uvicorn re-raising the signal
    # afterwards, which would replace the exit status below.
    runtime.request_exit = lambda: setattr(server, "should_exit", True)
    server.run()
    if runtime.polling_failed:
        logger.warning("exiting after telegram polling stopped")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return _migrate(args.revision)
    if args.command == "serve":
        return _serve()
    if args.command == "openapi":
        return _openapi(args.output)
    print(f"akkiabot {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
