from __future__ import annotations

from typing import ClassVar

import pytest

from akkiabot_e2e.config import E2EConfig, load_config
from akkiabot_e2e.errors import FloodWait
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import SCENARIOS, Scenario, ScenarioContext, select_scenarios
from akkiabot_e2e.testing import (
    ADMIN,
    BOT,
    MEMBER,
    MOCK_ADMIN_SESSION,
    MOCK_MEMBER_SESSION,
    Connector,
    FakeTelegram,
    live_env,
)
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS, UserInfo

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_LEAKY_BOT_TOKEN = "123456:bot-token-must-not-leak"


async def _no_sleep(_: float) -> None:
    return None


async def run(
    world: FakeTelegram,
    config: E2EConfig,
    *,
    scenarios: list[Scenario] | None = None,
    allow_create: bool = True,
    connector: Connector | None = None,
    pacer: Pacer | None = None,
    message_log=None,
) -> Report:
    return await run_acceptance(
        config,
        scenarios if scenarios is not None else list(SCENARIOS),
        connect=connector or Connector(world),
        run_id="test-run",
        allow_create=allow_create,
        pacer=pacer or Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        message_log=message_log,
    )


def statuses(report: Report) -> dict[str, str]:
    return {r.name: r.status for r in report.results}


async def test_first_run_creates_group_and_all_scenarios_pass(
    world: FakeTelegram, config: E2EConfig
) -> None:
    connector = Connector(world)
    report = await run(world, config, connector=connector)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert set(statuses(report).values()) == {"passed"}
    (chat,) = world.chats.values()
    assert report.chat == {"id": chat.id, "title": config.chat_title, "created": True}
    assert chat.members == {ADMIN.id: "creator", BOT.id: "admin", MEMBER.id: "member"}
    assert chat.rights[BOT.id] == BOT_ADMIN_RIGHTS
    assert any("allowed_chat_ids" in step for step in report.setup)
    assert all(account.closed for account in connector.accounts)


async def test_reuses_allowlisted_group_without_setup(world: FakeTelegram) -> None:
    chat = world.add_chat("AkkiaBot E2E 自动验收群", ADMIN)
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS | {"pin_messages"}
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=str(chat.id), E2E_REPLY_TIMEOUT="0.3"))

    report = await run(world, config, allow_create=False, scenarios=select_scenarios(["m2.help"]))

    assert report.status == "passed"
    assert report.setup == ["复用测试群"]
    assert len(world.chats) == 1
    assert "create_supergroup" not in world.calls
    assert "promote" not in world.calls


async def test_promote_keeps_existing_rights(world: FakeTelegram) -> None:
    chat = world.add_chat("AkkiaBot E2E 自动验收群", ADMIN)
    chat.members[BOT.id] = "admin"
    chat.rights[BOT.id] = frozenset({"pin_messages"})
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=str(chat.id)))

    await run(world, config, allow_create=False, scenarios=[])

    assert chat.rights[BOT.id] == BOT_ADMIN_RIGHTS | {"pin_messages"}


async def test_refuses_same_title_group_not_in_allowlist(
    world: FakeTelegram, config: E2EConfig
) -> None:
    chat = world.add_chat(config.chat_title, ADMIN)

    report = await run(world, config, allow_create=False)

    assert report.status == "failed"
    assert str(chat.id) in report.detail
    assert "E2E_ALLOWED_CHAT_IDS" in report.detail
    assert "send" not in world.calls


async def test_without_group_and_without_create_flag_sends_nothing(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await run(world, config, allow_create=False)

    assert report.status == "failed"
    assert "--create-group" in report.detail
    assert not world.chats
    assert "send" not in world.calls


async def test_other_users_group_is_never_used(world: FakeTelegram) -> None:
    stranger = world.add_user(UserInfo(9999, "路人"))
    chat = world.add_chat("AkkiaBot E2E 自动验收群", stranger)
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=str(chat.id)))

    report = await run(world, config, allow_create=False)

    assert report.status == "failed"
    assert "send" not in world.calls


async def test_refuses_accounts_outside_allowlist(world: FakeTelegram) -> None:
    config = load_config(live_env(E2E_ALLOWED_USER_IDS=f"{ADMIN.id},5555"))

    report = await run(world, config)

    assert report.status == "failed"
    assert str(MEMBER.id) in report.detail
    assert "E2E_ALLOWED_USER_IDS" in report.detail
    assert not world.chats


async def test_refuses_same_account_for_both_roles(world: FakeTelegram, config: E2EConfig) -> None:
    connector = Connector(world, {MOCK_ADMIN_SESSION: ADMIN, MOCK_MEMBER_SESSION: ADMIN})

    report = await run(world, config, connector=connector)

    assert "两个不同的测试账号" in report.detail
    assert all(account.closed for account in connector.accounts)


async def test_bot_that_does_not_answer_fails_with_timeout(
    world: FakeTelegram, config: E2EConfig
) -> None:
    assert world.bot is not None
    world.bot.silent = True

    report = await run(world, config, scenarios=select_scenarios(["m2.help"]))

    assert report.status == "failed"
    assert "没有收到 bot" in report.results[0].detail


async def test_unrendered_markdown_is_reported(world: FakeTelegram, config: E2EConfig) -> None:
    assert world.bot is not None
    world.bot.leave_escapes = True

    report = await run(world, config, scenarios=select_scenarios(["m2.id"]))

    assert statuses(report) == {"m2.id": "failed"}
    assert "反斜杠" in report.results[0].detail
    assert report.results[0].evidence  # the offending reply is kept as evidence


async def test_ip_unknown_field_fails(world: FakeTelegram, config: E2EConfig) -> None:
    assert world.bot is not None
    world.bot.ip_unknown_isp = True

    report = await run(world, config, scenarios=select_scenarios(["m3"]))

    assert statuses(report) == {"m3.ip": "failed", "m3.ip-reply": "failed"}
    assert "ISP" in report.results[0].detail


async def test_service_messages_left_behind_fail(world: FakeTelegram, config: E2EConfig) -> None:
    assert world.bot is not None
    world.bot.delete_service = False

    report = await run(world, config, scenarios=select_scenarios(["m2.service-message"]))

    assert statuses(report) == {"m2.service-message": "failed"}
    assert "没有被删除" in report.results[0].detail


async def test_flood_wait_is_honoured_then_retried(world: FakeTelegram, config: E2EConfig) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    world.floods["send"] = [7]
    report = await run(
        world,
        config,
        scenarios=select_scenarios(["m2.help"]),
        pacer=Pacer(max_wait=30, send_interval=0, sleep=sleep),
    )

    assert report.status == "passed"
    assert slept == [8]
    assert report.flood_wait_seconds == 8


async def test_long_flood_wait_stops_remaining_scenarios(
    world: FakeTelegram, config: E2EConfig
) -> None:
    world.floods["send"] = [3600]

    report = await run(
        world,
        config,
        scenarios=select_scenarios(["m2.help", "m2.id"]),
        pacer=Pacer(max_wait=60, send_interval=0, sleep=_no_sleep),
    )

    assert statuses(report) == {"m2.help": "failed", "m2.id": "not_run"}
    assert "3600" in report.results[0].detail
    assert report.status == "failed"


async def test_message_log_probe(world: FakeTelegram, config: E2EConfig) -> None:
    seen: list[list[int]] = []

    async def probe(chat_id: int, ids) -> set[int]:
        seen.append(list(ids))
        return set(ids[:-1])

    report = await run(
        world, config, scenarios=select_scenarios(["m2.id-reply", "m3.ip-reply"]), message_log=probe
    )

    assert len(seen[0]) == 2
    assert statuses(report)["m2.message-log"] == "failed"
    assert str(seen[0][-1]) in report.results[-1].detail


async def test_scenario_crash_is_error_and_secret_is_redacted(
    world: FakeTelegram, config: E2EConfig
) -> None:
    async def boom(ctx: ScenarioContext) -> None:
        raise RuntimeError(f"leaked {MOCK_ADMIN_SESSION}")

    report = await run(world, config, scenarios=[Scenario("x.boom", "X", "崩溃", boom)])

    assert statuses(report) == {"x.boom": "error"}
    redact = Redactor([MOCK_ADMIN_SESSION])
    assert MOCK_ADMIN_SESSION not in report.to_markdown(redact)
    assert MOCK_ADMIN_SESSION not in report.to_json(redact)


def test_select_scenarios() -> None:
    assert [s.name for s in select_scenarios(["m3"])] == ["m3.ip", "m3.ip-reply"]
    assert [s.name for s in select_scenarios(["m2.help,m3.ip"])] == ["m2.help", "m3.ip"]
    assert select_scenarios(None)[-1].name == "m2.service-message"
    with pytest.raises(ValueError, match="m9"):
        select_scenarios(["m9"])


def test_pacer_spaces_sends() -> None:
    import asyncio

    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    pacer = Pacer(send_interval=2.0, sleep=sleep, clock=lambda: now[0])

    async def go() -> None:
        await pacer.before_send()
        now[0] += 0.5
        await pacer.before_send()

    asyncio.run(go())
    assert slept == [1.5]


async def test_pacer_gives_up_after_max_retries() -> None:
    from akkiabot_e2e.errors import RateLimitExceeded

    async def always() -> None:
        raise FloodWait(1)

    with pytest.raises(RateLimitExceeded):
        await Pacer(max_retries=2, sleep=_no_sleep).call("x", always)


def test_cli_live_run_saves_private_redacted_report(
    world: FakeTelegram,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import stat

    from akkiabot_e2e import cli

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # A known secret that shows up in a bot reply (via the member's name) must be redacted.
    leaky = world.add_user(UserInfo(MEMBER.id, MOCK_LEAKY_BOT_TOKEN, MEMBER.username))
    connector = Connector(world, {MOCK_ADMIN_SESSION: ADMIN, MOCK_MEMBER_SESSION: leaky})
    monkeypatch.setattr(cli, "_telethon_connect", lambda api: connector)
    checked: list[str] = []

    async def fake_check(token, username, http) -> None:
        checked.append(username)

    class FakeVault:
        added: ClassVar[list[tuple[str, str, list[int]]]] = []

        def add_ids(self, item: str, field: str, ids) -> list[int]:
            self.added.append((item, field, list(ids)))
            return list(ids)

    monkeypatch.setattr("akkiabot_e2e.bot_api.check_test_bot", fake_check)
    monkeypatch.setattr(cli, "_vault", FakeVault)
    env = live_env(TELEGRAM_BOT_TOKEN=MOCK_LEAKY_BOT_TOKEN)

    code = cli.main(
        ["run", "--create-group", "--scenarios", "m2.id", "--report-dir", str(tmp_path / "r")],
        env=env,
    )

    out = capsys.readouterr().out
    (run_dir,) = (tmp_path / "r").iterdir()
    assert code == cli.EXIT_PASSED
    assert "真实 Telegram 验收：通过" in out
    for name in ("report.md", "report.json"):
        path = run_dir / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "bot-token-must-not-leak" not in path.read_text()
    assert "bot-token-must-not-leak" not in out
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    # Bot token was checked on the test endpoint first; the new group id was written back.
    assert checked == ["akkiabot_staging_bot"]
    (chat,) = world.chats.values()
    assert FakeVault.added == [("telegram-e2e", "allowed_chat_ids", [chat.id])]


@pytest.mark.parametrize("missing", ["leave", "join"])
async def test_missing_service_message_id_is_not_a_pass(
    world: FakeTelegram, config: E2EConfig, missing: str
) -> None:
    world.service_ids[missing] = False

    report = await run(world, config, scenarios=select_scenarios(["m2.service-message"]))

    assert statuses(report) == {"m2.service-message": "failed"}
    expected = "退群" if missing == "leave" else "入群"
    assert f"没有返回{expected}系统消息 id" in report.results[0].detail
    assert report.status == "failed"
