"""Alembic environment. Invoked through akkiabot_core.db.migrate, not alembic.ini."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection

from akkiabot_core.db.engine import create_engine
from akkiabot_core.db.models import Base
from akkiabot_core.db.types import UTCDateTime

config = context.config
target_metadata = Base.metadata


def _render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | Literal[False]:
    # Migrations should not import application types; UTCDateTime only
    # changes Python-side conversion, the column is a timezone-aware DATETIME.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=_render_item,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_with_new_engine(url: str) -> None:
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_configure)
            await conn.commit()
    finally:
        await engine.dispose()


def run() -> None:
    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            target_metadata=target_metadata,
            literal_binds=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
    else:
        url = config.get_main_option("sqlalchemy.url")
        assert url  # noqa: S101
        asyncio.run(_run_with_new_engine(url))


run()
