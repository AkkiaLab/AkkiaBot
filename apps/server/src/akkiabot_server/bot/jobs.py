"""JobQueue periodic tasks."""

from __future__ import annotations

import contextlib
from datetime import timedelta

from akkiabot_core.observability import log_context, new_trace_id, track_handler
from akkiabot_server.bot.deps import get_deps
from akkiabot_server.bot.types import App, Context

MESSAGE_LOG_CLEANUP = "message_log_cleanup"


@track_handler("job:" + MESSAGE_LOG_CLEANUP)
async def _cleanup_message_log(context: Context) -> None:
    deps = get_deps(context)
    retention = timedelta(days=deps.settings.telegram.message_log_retention_days)
    await deps.messages.cleanup(retention)


async def cleanup_message_log(context: Context) -> None:
    # Failures are counted and logged by track_handler; the next run retries.
    with log_context(trace_id=new_trace_id()), contextlib.suppress(Exception):
        await _cleanup_message_log(context)


def schedule_jobs(application: App, *, cleanup_interval: int) -> None:
    job_queue = application.job_queue
    if job_queue is None:  # pragma: no cover - the job-queue extra is a dependency
        raise RuntimeError("python-telegram-bot[job-queue] 未安装")
    job_queue.run_repeating(
        cleanup_message_log, interval=cleanup_interval, first=60, name=MESSAGE_LOG_CLEANUP
    )
