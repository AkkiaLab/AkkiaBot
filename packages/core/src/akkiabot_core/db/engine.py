from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine


def create_engine(url: str, *, echo: bool = False, **kwargs: Any) -> AsyncEngine:
    """Create the async engine for ``url`` (asyncpg or aiosqlite)."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "postgresql":
        kwargs.setdefault("pool_pre_ping", True)
    engine = _create_async_engine(url, echo=echo, **kwargs)
    if parsed.get_backend_name() == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """Unit of work: commit on success, roll back on error."""
    async with factory() as session, session.begin():
        yield session


async def ping(engine: AsyncEngine) -> bool:
    """Connectivity check for readiness probes."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
