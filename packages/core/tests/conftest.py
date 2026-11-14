"""Shared fixtures.

Backends beyond the in-process defaults are opt-in through environment
variables so the suite runs anywhere; CI sets both (see .github/workflows/ci.yml):

* ``AKKIABOT_TEST_DATABASE_URL`` — e.g. ``postgresql+asyncpg://akkiabot:akkiabot@127.0.0.1:5432/akkiabot``
  (the database is wiped: all tables are dropped before and after each test).
* ``AKKIABOT_TEST_REDIS_URL`` — e.g. ``redis://127.0.0.1:6379/15``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from akkiabot_core.db import create_engine

POSTGRES_URL = os.environ.get("AKKIABOT_TEST_DATABASE_URL")
REDIS_URL = os.environ.get("AKKIABOT_TEST_REDIS_URL")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty directory (no stray .env) with no app env vars."""
    for key in list(os.environ):
        if key.split("_", 1)[0] in {
            "TELEGRAM",
            "DB",
            "REDIS",
            "LLM",
            "TURNSTILE",
            "WEBAPP",
            "CORS",
            "OBSERVABILITY",
            "SERVER",
        }:
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def _db_params() -> list[object]:
    return [
        pytest.param("sqlite", id="sqlite"),
        pytest.param(
            "postgresql",
            id="postgresql",
            marks=pytest.mark.skipif(
                POSTGRES_URL is None, reason="AKKIABOT_TEST_DATABASE_URL 未设置"
            ),
        ),
    ]


async def _drop_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture(params=_db_params())
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite+aiosqlite:///{tmp_path / f'{uuid.uuid4().hex}.db'}"
    assert POSTGRES_URL is not None
    return POSTGRES_URL


@pytest.fixture
async def empty_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine on an empty database (no tables, no alembic_version)."""
    engine = create_engine(db_url)
    await _drop_all(engine)
    try:
        yield engine
    finally:
        await _drop_all(engine)
        await engine.dispose()
