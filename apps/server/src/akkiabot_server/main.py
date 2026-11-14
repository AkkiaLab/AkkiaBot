"""Command-line entry point of the AkkiaBot server.

* ``akkiabot`` / ``akkiabot --version`` — print the version (container smoke test).
* ``akkiabot migrate [REVISION]`` — apply Alembic migrations (default ``head``)
  to the database configured by ``DB_URL``. Run it before starting the app.
* ``akkiabot serve`` — run the bot and the HTTP API in one event loop.
* ``akkiabot openapi [--output FILE]`` — write the OpenAPI document (used by
  ``just gen-api``); needs no configuration.
* ``akkiabot import-legacy LEGACY_DB [--dry-run] [--report FILE]`` — one-shot
  import of group settings and warning counts from a read-only copy of the Go
  botlab-core SQLite file (M10, docs/operations.md §9). Prints a diff report.
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
from akkiabot_core.db import create_engine, create_session_factory
from akkiabot_core.db.migrate import upgrade
from akkiabot_core.observability import configure_logging, setup_observability
from akkiabot_server import __version__

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
    legacy = sub.add_parser(
        "import-legacy",
        help="从 Go 版 botlab-core 的 SQLite 只读副本导入群配置与警告数（M10）",
    )
    legacy.add_argument("legacy_db", type=Path, help="旧库 sqlite.db 的只读副本")
    legacy.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出差异报告，不写入；未配置 DB_URL 时按空库计算",
    )
    legacy.add_argument(
        "--report", type=Path, help="另外把差异写成 JSON 文件（必须是不存在的新文件）"
    )
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

    setup_observability(settings.observability, release=f"akkiabot@{__version__}")
    logger.info("starting akkiabot", extra={"version": __version__, "config": settings.redacted()})
    app = create_app(Runtime.build(settings))
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,  # keep our structured logging
        access_log=False,
        lifespan="on",
        server_header=False,
    )
    uvicorn.Server(config).run()
    return 0


def _import_legacy(legacy_db: Path, *, dry_run: bool, report: Path | None) -> int:
    """Exit codes: 0 ok, 1 statistics mismatch (nothing written), 2 bad input/config."""
    import asyncio
    import os
    import sys

    from akkiabot_server import legacy_import

    obs = load_observability_settings()
    # Logs go to stderr so stdout carries only the report.
    configure_logging(obs.log_level, fmt=obs.log_format, stream=sys.stderr)
    if report is not None:
        # Validate before reading or writing anything: the report must never
        # replace the legacy database (same path, symlink or hard link).
        try:
            legacy_import.check_report_path(report, legacy_db)
        except legacy_import.LegacyImportError as exc:
            logger.error("legacy import failed: %s", exc)
            return 2
    db_url = None
    if os.environ.get("DB_URL"):
        try:
            db_url = load_database_settings().url
        except ValidationError as exc:
            logger.error("invalid database configuration: %s", exc)
            return 2

    async def run() -> legacy_import.ImportPlan:
        snapshot = legacy_import.read_legacy(legacy_db)
        if db_url is None:
            return await legacy_import.run_import(snapshot, None, dry_run=dry_run)
        engine = create_engine(db_url)
        try:
            factory = create_session_factory(engine)
            return await legacy_import.run_import(snapshot, factory, dry_run=dry_run)
        finally:
            await engine.dispose()

    try:
        plan = asyncio.run(run())
    except legacy_import.LegacyImportError as exc:
        logger.error("legacy import failed: %s", exc)
        return 2
    print(legacy_import.render_report(plan, source=legacy_db, dry_run=dry_run), end="")
    if report is not None:
        data = legacy_import.plan_to_json(plan, source=legacy_db, dry_run=dry_run)
        # "x": never overwrite, even if the file appeared after the check.
        with report.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if not plan.consistent:
        logger.error("legacy import aborted: source statistics do not match the plan")
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
    if args.command == "import-legacy":
        return _import_legacy(args.legacy_db, dry_run=args.dry_run, report=args.report)
    print(f"akkiabot {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
