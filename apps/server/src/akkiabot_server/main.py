"""Command-line entry point of the AkkiaBot server.

* ``akkiabot`` / ``akkiabot --version`` — print the version (container smoke test).
* ``akkiabot migrate [REVISION]`` — apply Alembic migrations (default ``head``)
  to the database configured by ``DB_URL``. Run it before starting the app.

The bot runtime and HTTP API arrive in M2.
"""

import argparse
import logging
from collections.abc import Sequence

from pydantic import ValidationError

from akkiabot_core.config import load_database_settings, load_observability_settings
from akkiabot_core.db.migrate import upgrade
from akkiabot_core.observability import configure_logging
from akkiabot_server import __version__

logger = logging.getLogger("akkiabot_server")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akkiabot", description="AkkiaBot server")
    parser.add_argument("--version", action="version", version=f"akkiabot {__version__}")
    sub = parser.add_subparsers(dest="command")
    migrate = sub.add_parser("migrate", help="执行数据库迁移（读取 DB_URL）")
    migrate.add_argument("revision", nargs="?", default="head", help="目标版本，默认 head")
    return parser


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "migrate":
        return _migrate(args.revision)
    print(f"akkiabot {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
