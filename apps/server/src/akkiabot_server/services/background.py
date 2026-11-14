"""Fire-and-forget work that must not block update processing.

PTB processes updates one after another, so slow work (LLM calls, delayed
clean-ups) runs as tasks owned by :class:`BackgroundTasks`. Tasks inherit the
caller's context variables (log context), exceptions are logged instead of
being lost, and the runtime cancels whatever is left on shutdown. Tests call
:meth:`drain` to wait for everything, including tasks spawned by tasks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class BackgroundTasks:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
        """Run ``coro`` in the background; returns ``None`` once closed."""
        if self._closed:
            coro.close()
            logger.warning("background task dropped during shutdown", extra={"task": name})
            return None
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "background task failed",
                extra={"task": task.get_name()},
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def __len__(self) -> int:
        return len(self._tasks)

    async def drain(self) -> None:
        """Wait until no task is left (tasks may spawn more tasks)."""
        while self._tasks:
            pending = [task for task in self._tasks if not task.done()]
            if pending:
                await asyncio.wait(pending)
            else:
                # Finished tasks leave the set from their done callbacks: let them run.
                await asyncio.sleep(0)

    async def aclose(self) -> None:
        """Refuse new tasks and cancel the pending ones."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            logger.info("cancelled pending background tasks", extra={"count": len(tasks)})
            await asyncio.gather(*tasks, return_exceptions=True)
