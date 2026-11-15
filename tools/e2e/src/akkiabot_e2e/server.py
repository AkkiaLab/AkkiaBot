"""Run the AkkiaBot server under test as a child process (``run --serve``).

The bot gets a fresh SQLite database and in-process cache in the run
directory, binds HTTP and metrics to loopback, and never sees the E2E user
sessions or the 1Password service account token.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Self

import httpx

from akkiabot_e2e.errors import E2EError

_STRIPPED_PREFIXES = ("E2E_", "OP_")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_env(
    base: Mapping[str, str], *, db_path: Path, port: int, metrics_port: int
) -> dict[str, str]:
    """Environment for the bot: business secrets yes, user sessions / op token no."""
    env = {k: v for k, v in base.items() if not k.startswith(_STRIPPED_PREFIXES)}
    env.update(
        DB_URL=f"sqlite+aiosqlite:///{db_path}",
        REDIS_URL="",
        TELEGRAM_MODE="polling",
        # The bot under test always talks to the Telegram test environment.
        TELEGRAM_TEST_ENVIRONMENT="true",
        SERVER_HOST="127.0.0.1",
        SERVER_PORT=str(port),
        OBSERVABILITY_METRICS_HOST="127.0.0.1",
        OBSERVABILITY_METRICS_PORT=str(metrics_port),
        OBSERVABILITY_LOG_FORMAT="json",
    )
    env.setdefault("OBSERVABILITY_SENTRY_ENVIRONMENT", "staging")
    return env


class ManagedServer:
    """``akkiabot migrate`` + ``akkiabot serve``; output goes to ``server.log`` (0600)."""

    def __init__(
        self,
        workdir: Path,
        base_env: Mapping[str, str],
        *,
        command: Sequence[str] = ("akkiabot",),
        ready_timeout: float = 60.0,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.workdir = workdir
        self.db_path = workdir / "bot.sqlite3"
        self.log_path = workdir / "server.log"
        self.port = free_port()
        self.env = server_env(
            base_env, db_path=self.db_path, port=self.port, metrics_port=free_port()
        )
        # Run-specific settings (e.g. the local Mini App with Turnstile test keys).
        self.env.update(extra_env or {})
        self.command = list(command)
        self.ready_timeout = ready_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None

    async def __aenter__(self) -> Self:
        fd = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._log = log = os.fdopen(fd, "ab")
        try:
            await self._start(log)
        except BaseException:
            await self.stop()
            raise
        return self

    async def _start(self, log: IO[bytes]) -> None:
        migrate = await asyncio.to_thread(
            subprocess.run,
            [*self.command, "migrate"],
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if migrate.returncode != 0:
            raise E2EError(
                f"akkiabot migrate 失败（退出码 {migrate.returncode}），见 {self.log_path}"
            )
        self._process = subprocess.Popen(  # noqa: S603, ASYNC220 - long-lived child, fixed argv
            [*self.command, "serve"], env=self.env, stdout=log, stderr=subprocess.STDOUT
        )
        await self._wait_ready()

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    @property
    def api_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def restart(self, overrides: Mapping[str, str]) -> None:
        """Stop ``serve``, apply ``overrides`` and start it again on the same
        port and database (migrations are re-run, a no-op)."""
        log = self._log
        if log is None:
            raise E2EError("bot 进程尚未启动")
        process, self._process = self._process, None
        if process is not None:
            await _terminate(process)
        self.env.update(overrides)
        await self._start(log)

    async def _wait_ready(self) -> None:
        process = self._process
        if process is None:
            raise E2EError("bot 进程尚未启动")
        url = f"http://127.0.0.1:{self.port}/readyz"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                if process.poll() is not None:
                    raise E2EError(
                        f"bot 进程提前退出（退出码 {process.returncode}），见 {self.log_path}"
                    )
                with contextlib.suppress(httpx.HTTPError):
                    if (await client.get(url)).status_code == 200:
                        return
                if loop.time() >= deadline:
                    raise E2EError(
                        f"bot 在 {self.ready_timeout:g} 秒内没有就绪，见 {self.log_path}"
                    )
                await asyncio.sleep(0.5)

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            await _terminate(process)
        if self._log is not None:
            self._log.close()
            self._log = None

    async def logged_message_ids(self, chat_id: int, message_ids: Sequence[int]) -> set[int]:
        return await asyncio.to_thread(query_message_log, self.db_path, chat_id, message_ids)


async def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        await asyncio.to_thread(process.wait, 20)
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.wait)


def query_message_log(db_path: Path, chat_id: int, message_ids: Sequence[int]) -> set[int]:
    if not message_ids:
        return set()
    placeholders = ",".join("?" * len(message_ids))
    sql = (
        "SELECT message_id FROM message_log "  # noqa: S608 - placeholders only
        f"WHERE chat_id = ? AND message_id IN ({placeholders})"
    )
    with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute(sql, (chat_id, *message_ids)).fetchall()
    return {int(row[0]) for row in rows}
