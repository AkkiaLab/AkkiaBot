"""Repeated runs against a bot whose database persists (staging, BOT-27).

The fake bot keeps per-chat member message counts across runs, like the
deployed staging bot does in PostgreSQL.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from akkiabot_e2e.config import E2EConfig
from akkiabot_e2e.group import fresh_group_title
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import FRESH_GROUP_NOTE, run_acceptance
from akkiabot_e2e.scenarios import select_scenarios
from akkiabot_e2e.testing import (
    ADMIN,
    MEMBER,
    Connector,
    FakeRegistry,
    FakeTelegram,
    FakeTraces,
    FakeWeb,
    live_env,
)

M5 = ["m5.spam", "m5.langfuse"]


async def _no_sleep(_: float) -> None:
    return None


async def _run(
    world: FakeTelegram,
    config: E2EConfig,
    registry: FakeRegistry,
    run_id: str,
    *,
    fresh_group: bool,
    scenarios: list[str] | None = None,
) -> Report:
    return await run_acceptance(
        config,
        select_scenarios(scenarios or M5),
        connect=Connector(world),
        run_id=run_id,
        allow_create=True,
        pacer=Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        traces=FakeTraces(),
        registry=registry,
        web=FakeWeb(world),
        fresh_group=fresh_group,
    )


def _chat(report: Report) -> dict[str, Any]:
    assert report.chat is not None
    return report.chat


def _statuses(report: Report) -> dict[str, str]:
    return {r.name: r.status for r in report.results}


async def test_reused_group_exempts_the_member_on_a_later_run(
    world: FakeTelegram, config: E2EConfig
) -> None:
    """Reproduces the staging failure: the member's count carries over between runs."""
    registry = FakeRegistry()
    first = await _run(world, config, registry, "run-1", fresh_group=False, scenarios=["m5", "m2"])
    assert first.status == "passed"
    # The allowlist now holds the group, as 1Password does on staging.
    reused = replace(config, allowed_chat_ids=frozenset(registry.allowed))
    second = await _run(world, reused, registry, "run-2", fresh_group=False)
    assert _chat(second)["id"] == _chat(first)["id"]
    assert _statuses(second)["m5.spam"] == "failed"  # exempt: more than 3 messages
    assert world.chats[_chat(second)["id"]].message_counts[MEMBER.id] > 3


async def test_fresh_group_makes_m5_repeatable(world: FakeTelegram, config: E2EConfig) -> None:
    registry = FakeRegistry()
    reports = [
        await _run(world, config, registry, run_id, fresh_group=True, scenarios=["m5", "m2"])
        for run_id in ("run-1", "run-2", "run-3")
    ]
    for report in reports:
        assert report.status == "passed", _statuses(report)
        assert _chat(report)["created"] is True
        assert report.setup[0] == FRESH_GROUP_NOTE
    ids = [_chat(r)["id"] for r in reports]
    assert len(set(ids)) == 3
    assert registry.allowed == set(ids)
    assert registry.pending is None
    titles = [_chat(r)["title"] for r in reports]
    assert titles == [fresh_group_title(config.chat_title, f"run-{i}") for i in (1, 2, 3)]


async def test_fresh_group_never_adopts_the_allowlisted_group(
    world: FakeTelegram, config: E2EConfig
) -> None:
    old = world.add_chat(config.chat_title, ADMIN)
    registry = FakeRegistry({old.id})
    allowed = replace(config, allowed_chat_ids=frozenset({old.id}))
    report = await _run(world, allowed, registry, "run-1", fresh_group=True)
    assert report.status == "passed"
    assert _chat(report)["id"] != old.id
    assert registry.allowed == {old.id, _chat(report)["id"]}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["run", "--scenarios", "m5"], False),
        (["run", "--fresh-group", "--scenarios", "m5"], True),
        (["run", "--record-status", "a" * 40], True),
    ],
)
def test_cli_fresh_group_flag(
    tmp_path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    from akkiabot_e2e import cli

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen: dict[str, Any] = {}

    async def fake_run_acceptance(config, scenarios, **kwargs: Any) -> Report:
        seen.update(kwargs)
        return Report(run_id=kwargs["run_id"], status="passed", started_at="-")

    async def fake_guarded_run(sha, url, web, run) -> Report:
        return await run()

    async def fake_check(token, username, http) -> None:
        return None

    monkeypatch.setattr(cli, "run_acceptance", fake_run_acceptance)
    monkeypatch.setattr(cli, "guarded_run", fake_guarded_run)
    monkeypatch.setattr(cli, "record_status", lambda sha, report, repo: "success")
    monkeypatch.setattr(cli, "_vault", lambda: None)
    monkeypatch.setattr("akkiabot_e2e.bot_api.check_test_bot", fake_check)
    env = live_env(E2E_WEBAPP_URL="https://webapp.example.test")

    code = cli.main([*argv, "--report-dir", str(tmp_path / "r")], env=env)

    assert code == cli.EXIT_PASSED
    assert seen["fresh_group"] is expected
