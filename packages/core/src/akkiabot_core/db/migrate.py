"""Programmatic Alembic entry points (used by ``akkiabot migrate``)."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

logger = logging.getLogger(__name__)


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("path_separator", "os")
    if url is not None:
        # ConfigParser interpolation treats "%" specially.
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at ``url``. Must not be called inside a running loop."""
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def revision(url: str, message: str, *, autogenerate: bool = True) -> None:
    """Create a new revision file (developer helper, see ``just db-revision``)."""
    command.revision(alembic_config(url), message=message, autogenerate=autogenerate)


async def upgrade_engine(engine: AsyncEngine, revision: str = "head") -> None:
    """Upgrade using an existing async engine (usable from async code / tests)."""

    def _run(connection: Connection) -> None:
        cfg = alembic_config()
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)

    async with engine.begin() as conn:
        await conn.run_sync(_run)
