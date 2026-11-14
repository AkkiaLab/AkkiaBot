"""Orchestrates one live acceptance run (transport-agnostic, unit-tested with fakes)."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from akkiabot_e2e.config import E2EConfig
from akkiabot_e2e.errors import (
    CheckFailed,
    E2EError,
    RateLimitExceeded,
    SafetyError,
    ScenarioSkipped,
)
from akkiabot_e2e.group import prepare_group
from akkiabot_e2e.langfuse import LangfuseProbe
from akkiabot_e2e.pacing import PacedAccount, Pacer
from akkiabot_e2e.redaction import Secret
from akkiabot_e2e.report import Report, ScenarioResult
from akkiabot_e2e.scenarios import Scenario, ScenarioContext, TraceProbe
from akkiabot_e2e.transport import Account, UserInfo

logger = logging.getLogger(__name__)

#: Opens a logged-in account for ``role`` ("admin" / "member") from its session.
Connect = Callable[[str, Secret], Awaitable[Account]]
#: Returns which of the given message ids the bot stored in ``message_log`` for a chat.
MessageLogProbe = Callable[[int, Sequence[int]], Awaitable[set[int]]]


def check_identities(config: E2EConfig, admin: UserInfo, member: UserInfo) -> None:
    for role, user in (("群主/管理员", admin), ("普通成员", member)):
        if user.is_bot:
            raise SafetyError(f"{role}会话登录的是 bot，需要真实用户账号")
        if user.id not in config.allowed_user_ids:
            raise SafetyError(
                f"{role}会话的账号 {user.id} 不在 E2E_ALLOWED_USER_IDS 中，拒绝用它发消息"
            )
    if admin.id == member.id:
        raise SafetyError("群主与普通成员必须是两个不同的测试账号")


def _user_dict(user: UserInfo) -> dict[str, object]:
    return {"id": user.id, "first_name": user.first_name, "username": user.username}


async def run_acceptance(
    config: E2EConfig,
    scenarios: Sequence[Scenario],
    *,
    connect: Connect,
    run_id: str,
    allow_create: bool = False,
    pacer: Pacer | None = None,
    poll_interval: float = 1.0,
    message_log: MessageLogProbe | None = None,
    traces: TraceProbe | None = None,
) -> Report:
    """``traces`` overrides the Langfuse probe built from ``config.langfuse`` (tests)."""
    pacer = pacer or Pacer(max_wait=config.flood_max_wait, send_interval=config.send_interval)
    report = Report(run_id=run_id, status="failed", started_at=datetime.now(UTC).isoformat())
    async with AsyncExitStack() as stack:
        try:
            admin = PacedAccount(await connect("admin", config.admin_session), pacer, "admin")
            stack.push_async_callback(admin.close)
            member = PacedAccount(await connect("member", config.member_session), pacer, "member")
            stack.push_async_callback(member.close)

            admin_user, member_user = await admin.me(), await member.me()
            check_identities(config, admin_user, member_user)
            bot = await admin.resolve_user(config.bot_username)
            if not bot.is_bot:
                raise SafetyError(f"@{config.bot_username} 不是 bot 账号")
            report.accounts = {
                "群主/管理员": _user_dict(admin_user),
                "普通成员": _user_dict(member_user),
                "测试 bot": _user_dict(bot),
            }
            group = await prepare_group(
                admin,
                member,
                admin_user=admin_user,
                member_user=member_user,
                bot=bot,
                config=config,
                allow_create=allow_create,
            )
        except (E2EError, OSError) as exc:
            report.detail = f"准备阶段失败：{exc}"
            report.flood_wait_seconds = pacer.waited
            report.finish()
            return report

        report.chat = {"id": group.chat.id, "title": group.chat.title, "created": group.created}
        report.setup = list(group.actions)
        if group.created:
            report.setup.append(
                f"新建的群 id {group.chat.id} 需写入 1Password telegram-e2e/allowed_chat_ids"
            )
        ctx = ScenarioContext(
            admin=admin,
            member=member,
            admin_user=admin_user,
            member_user=member_user,
            bot=bot,
            chat=group.chat,
            run_id=run_id,
            reply_timeout=config.reply_timeout,
            poll_interval=poll_interval,
            antispam=config.antispam,
            antispam_wait=config.antispam_wait,
        )
        if traces is None and config.langfuse is not None:
            probe = LangfuseProbe(config.langfuse)
            stack.push_async_callback(probe.aclose)
            traces = probe
        ctx.traces = traces
        await _run_scenarios(ctx, scenarios, report)
        if message_log is not None and ctx.plain_message_ids:
            report.results.append(await _check_message_log(ctx, message_log))
    report.flood_wait_seconds = pacer.waited
    report.finish()
    return report


async def _run_scenarios(
    ctx: ScenarioContext, scenarios: Sequence[Scenario], report: Report
) -> None:
    aborted: str | None = None
    for scenario in scenarios:
        result = ScenarioResult(
            scenario.name, scenario.milestone, scenario.description, status="not_run"
        )
        report.results.append(result)
        if aborted:
            result.detail = aborted
            continue
        ctx.evidence = []
        started = time.monotonic()
        try:
            await scenario.run(ctx)
            result.status = "passed"
        except ScenarioSkipped as exc:
            result.status, result.detail = "not_run", str(exc)
        except CheckFailed as exc:
            result.status, result.detail = "failed", str(exc)
        except RateLimitExceeded as exc:
            result.status, result.detail = "failed", str(exc)
            aborted = "因限流中止，未执行"
        except Exception as exc:
            logger.exception("scenario %s crashed", scenario.name)
            result.status, result.detail = "error", f"{type(exc).__name__}: {exc}"
        result.duration = round(time.monotonic() - started, 2)
        result.evidence = ctx.evidence


async def _check_message_log(ctx: ScenarioContext, probe: MessageLogProbe) -> ScenarioResult:
    result = ScenarioResult(
        "m2.message-log", "M2", "群消息被记录到 message_log（只存 ID）", status="failed"
    )
    expected = list(ctx.plain_message_ids)
    try:
        found = await probe(ctx.chat.id, expected)
    except Exception as exc:
        result.status, result.detail = "error", f"{type(exc).__name__}: {exc}"
        return result
    missing = [i for i in expected if i not in found]
    result.evidence = [{"step": "查询 message_log", "expected": expected, "found": sorted(found)}]
    if missing:
        result.detail = f"message_log 缺少消息 {missing}"
    else:
        result.status = "passed"
    return result
