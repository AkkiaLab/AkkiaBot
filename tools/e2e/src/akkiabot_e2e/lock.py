"""Machine-local lock: one live run (one use of the test sessions) at a time."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
from collections.abc import AsyncIterator
from pathlib import Path

from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.files import ensure_private_dir


@contextlib.asynccontextmanager
async def run_lock(
    path: Path, *, wait_timeout: float = 600.0, poll: float = 2.0
) -> AsyncIterator[None]:
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_timeout
        announced = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if loop.time() >= deadline:
                    raise E2EError(
                        f"等待验收锁超过 {wait_timeout:g} 秒（{path}），另一个验收进程仍在运行"
                    ) from None
                if not announced:
                    print(f"另一个验收进程正在使用测试账号会话，等待释放（{path}）…", flush=True)
                    announced = True
                await asyncio.sleep(poll)
        yield
    finally:
        os.close(fd)
