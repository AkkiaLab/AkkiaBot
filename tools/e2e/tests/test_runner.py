from __future__ import annotations

from typing import ClassVar

import pytest

from akkiabot_e2e.config import E2EConfig, load_config
from akkiabot_e2e.errors import FloodWait
from akkiabot_e2e.group import operation_marker
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import (
    SCENARIOS,
    Scenario,
    ScenarioContext,
    restore_member,
    select_scenarios,
)
from akkiabot_e2e.testing import (
    ADMIN,
    BOT,
    MEMBER,
    MOCK_ADMIN_SESSION,
    MOCK_MEMBER_SESSION,
    Connector,
    FakeRegistry,
    FakeTelegram,
    FakeTraces,
    FakeWeb,
    live_env,
)
from akkiabot_e2e.transport import BOT_ADMIN_RIGHTS, ChatRef, UserInfo

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
    traces: FakeTraces | None = None,
    registry: FakeRegistry | None = None,
    web: FakeWeb | None = None,
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
        traces=traces or FakeTraces(),
        registry=registry or FakeRegistry(),
        web=web or FakeWeb(world),
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
    (chat,) = [c for c in world.chats.values() if c.id < 0]  # plus the private chat with the bot
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
    world.contacts[MEMBER.id].add(ADMIN.id)  # set up by an earlier run
    world.contacts[ADMIN.id].add(MEMBER.id)

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

    assert statuses(report) == {"m2.help": "failed", "m2.id": "skipped"}
    assert report.results[1].skip_reason == "rate_limited"
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
    assert [s.name for s in select_scenarios(["m4"])] == [
        "m4.permissions",
        "m4.config",
        "m4.warn",
        "m4.ban",
    ]
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
        log: ClassVar[list[tuple[str, ...]]] = []

        def read(self, item: str, field: str) -> str | None:
            self.log.append(("read", item, field))
            return None

        def set_fields(self, item: str, values) -> None:
            self.log.extend(("set", item, k, v.value) for k, v in values.items())

        def add_ids(self, item: str, field: str, ids) -> list[int]:
            self.log.append(("add_ids", item, field, *map(str, ids)))
            return list(ids)

        def clear_field(self, item: str, field: str) -> None:
            self.log.append(("clear", item, field))

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
    (chat,) = [c for c in world.chats.values() if c.id < 0]
    # Pending operation recorded before creating, id registered, then the operation cleared.
    read, begin, register, clear = FakeVault.log
    assert read == ("read", "e2e-telegram", "pending_group_operation")
    assert begin[:3] == ("set", "e2e-telegram", "pending_group_operation")
    assert operation_marker(begin[3]) in chat.about
    assert register == ("add_ids", "e2e-telegram", "allowed_chat_ids", str(chat.id))
    assert clear == ("clear", "e2e-telegram", "pending_group_operation")


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


# ------------------------------------------------------------------ M4


def _group_chat(world: FakeTelegram) -> int:
    (chat,) = [c for c in world.chats.values() if c.id < 0]
    return chat.id


async def test_m4_scenarios_pass_and_restore_the_member(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await run(world, config, scenarios=select_scenarios(["m4", "m2.help"]))
    assert report.status == "passed", report.to_markdown(Redactor())
    chat = world.chats[_group_chat(world)]
    # Banned twice (warn limit, /ban) and restored each time, so later scenarios still work.
    assert chat.members[MEMBER.id] == "member"
    assert chat.banned == set()
    assert world.calls.count("unban") == 2
    assert world.bot is not None
    assert world.bot.settings[chat.id]["warn"] is True


async def test_m4_warn_enables_the_switch_when_run_alone(
    world: FakeTelegram, config: E2EConfig
) -> None:
    report = await run(world, config, scenarios=select_scenarios(["m4.warn"]))
    assert statuses(report) == {"m4.warn": "passed"}, report.to_markdown(Redactor())


@pytest.mark.parametrize(
    ("knob", "value", "scenario", "detail"),
    [
        ("members_can_moderate", True, "m4.permissions", "普通成员 /config 应被拒绝"),
        ("stale_menu", True, "m4.config", "菜单在"),
        ("no_purge", True, "m4.ban", "只删除了 0 条消息"),
        ("no_purge", True, "m4.warn", "没有被删除"),
        ("no_ban", True, "m4.ban", "只删除了 0 条消息"),
        ("no_ban", True, "m4.warn", "封禁列表"),
        ("full_names", True, "m4.ban", "@username"),
        ("full_names", True, "m4.warn", "@username"),
        ("warn_limit", 2, "m4.permissions", None),  # a different limit is still fine
    ],
)
async def test_m4_regressions_are_caught(
    world: FakeTelegram,
    config: E2EConfig,
    knob: str,
    value: object,
    scenario: str,
    detail: str | None,
) -> None:
    assert world.bot is not None
    setattr(world.bot, knob, value)
    report = await run(world, config, scenarios=select_scenarios([scenario]))
    (result,) = report.results
    if detail is None:
        assert result.status == "passed", report.to_markdown(Redactor())
        return
    assert result.status == "failed", report.to_markdown(Redactor())
    assert detail in (result.detail or "")
    # Whatever failed, the member is back in the group.
    chat = world.chats[_group_chat(world)]
    assert chat.members.get(MEMBER.id) == "member"


async def test_member_left_banned_by_an_interrupted_run_is_restored(world: FakeTelegram) -> None:
    chat = world.add_chat("AkkiaBot E2E 自动验收群", ADMIN)
    chat.members[BOT.id] = "admin"
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS
    chat.banned.add(MEMBER.id)
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=str(chat.id)))

    report = await run(world, config, allow_create=False, scenarios=select_scenarios(["m2.help"]))

    assert report.status == "passed", report.to_markdown(Redactor())
    assert any("解除普通成员账号的封禁" in step for step in report.setup)
    assert chat.banned == set()
    assert chat.members[MEMBER.id] == "member"


# ---------------------------------------------------------------------- M5


async def test_m5_scenarios_pass_and_leave_the_member_free(
    world: FakeTelegram, config: E2EConfig
) -> None:
    traces = FakeTraces()
    report = await run(world, config, scenarios=select_scenarios(["m5"]), traces=traces)
    assert report.status == "passed", report.to_markdown(Redactor())
    chat = world.chats[_group_chat(world)]
    assert chat.restricted == set()
    assert chat.banned == set()
    assert chat.members[MEMBER.id] == "member"
    assert world.bot is not None
    assert world.bot.settings[chat.id]["antispam"] is False  # switched off again
    assert traces.asked == ["spam test-run"]


async def test_m5_skipped_without_llm(world: FakeTelegram) -> None:
    env = live_env(LLM_API_KEY="", LLM_MODEL="")
    report = await run(world, load_config(env), scenarios=select_scenarios(["m5.spam", "m2.help"]))
    assert statuses(report) == {"m5.spam": "skipped", "m2.help": "passed"}
    assert "未配置 LLM" in report.results[0].detail
    assert report.status == "failed"  # M5 was not verified


async def test_m5_langfuse_skipped_without_probe(world: FakeTelegram, config: E2EConfig) -> None:
    report = await run_acceptance(
        config,
        select_scenarios(["m5.spam", "m5.langfuse"]),
        connect=Connector(world),
        run_id="test-run",
        allow_create=True,
        pacer=Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        registry=FakeRegistry(),
    )
    assert statuses(report) == {"m5.spam": "passed", "m5.langfuse": "skipped"}
    assert "LANGFUSE" in report.results[1].detail


async def test_m5_missing_trace_fails(world: FakeTelegram, config: E2EConfig) -> None:
    report = await run(
        world,
        config,
        scenarios=select_scenarios(["m5.spam", "m5.langfuse"]),
        traces=FakeTraces(set()),
    )
    assert statuses(report)["m5.langfuse"] == "failed"
    assert "spam test-run" in report.results[1].detail


@pytest.mark.parametrize(
    ("knob", "scenario", "detail"),
    [
        ("antispam_ignores_switch", "m5.disabled", "反垃圾已关闭，但消息"),
        ("antispam_no_exemption", "m5.exempt", "老成员应被豁免，但消息"),
        ("antispam_no_mute", "m5.spam", "成员没有被禁言"),
        ("full_names", "m5.spam", "@username"),
    ],
)
async def test_m5_regressions_are_caught(
    world: FakeTelegram, config: E2EConfig, knob: str, scenario: str, detail: str
) -> None:
    assert world.bot is not None
    setattr(world.bot, knob, True)
    report = await run(world, config, scenarios=select_scenarios([scenario]))
    (result,) = report.results
    assert result.status == "failed", report.to_markdown(Redactor())
    assert detail in result.detail
    chat = world.chats[_group_chat(world)]
    assert chat.restricted == set()  # the member is never left muted


async def test_m5_spam_command(world: FakeTelegram, config: E2EConfig) -> None:
    report = await run(world, config, scenarios=select_scenarios(["m5.spam-command"]))
    assert report.status == "passed", report.to_markdown(Redactor())
    chat = world.chats[_group_chat(world)]
    assert chat.members[MEMBER.id] == "member"


async def test_member_left_muted_by_an_interrupted_run_is_restored(world: FakeTelegram) -> None:
    chat = world.add_chat("AkkiaBot E2E 自动验收群", ADMIN)
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS
    chat.restricted.add(MEMBER.id)
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=str(chat.id)))

    report = await run(world, config, allow_create=False, scenarios=select_scenarios(["m2.help"]))

    assert report.status == "passed", report.to_markdown(Redactor())
    assert any("解除普通成员账号的禁言" in step for step in report.setup)
    assert chat.restricted == set()


# ------------------------------------------------- M5: asynchronous side effects (BOT-47)
#
# The real guard acts in the background: the LLM verdict may take up to
# ANTISPAM_TIMEOUT (45 s), and the spammer's history is purged
# ANTISPAM_PURGE_DELAY (5 s) after the mute. The fake bot defers those side
# effects by a number of account operations (``spam_lag`` / ``purge_lag``), so
# these runs are deterministic. Scenarios run in catalog order: m5.spam first,
# then m4.warn, as in the staging release acceptance of BOT-40.

SPAM_THEN_WARN = ["m5.spam", "m4.warn"]


def _report(report: Report) -> str:
    return report.to_markdown(Redactor())


async def test_m5_late_purge_waits_and_does_not_hit_the_next_scenario(
    world: FakeTelegram, config: E2EConfig
) -> None:
    # RC1: the purge came after m5.spam had passed and deleted m4.warn's messages.
    assert world.bot is not None
    world.bot.purge_lag = 12  # lands in the middle of m4.warn without the wait
    report = await run(world, config, scenarios=select_scenarios(SPAM_THEN_WARN))
    assert statuses(report) == {"m5.spam": "passed", "m4.warn": "passed"}, _report(report)
    assert world.pending_side_effects == 0
    steps = [e["step"] for e in report.results[0].evidence]
    assert any("反垃圾清理历史" in step for step in steps)


async def test_m5_slow_verdict_within_budget_is_awaited(world: FakeTelegram) -> None:
    # RC2: the verdict took longer than E2E_REPLY_TIMEOUT but stayed within the
    # bot's classification budget; m5.spam gave up and the late mute and purge
    # hit m4.warn.
    assert world.bot is not None
    # More polls than fit in E2E_REPLY_TIMEOUT (0.3 s / 0.01 s), far fewer than
    # fit in E2E_ANTISPAM_TIMEOUT.
    world.bot.spam_lag = 50
    world.bot.purge_lag = 12
    config = load_config(live_env(E2E_ANTISPAM_TIMEOUT="10"))
    report = await run(world, config, scenarios=select_scenarios(SPAM_THEN_WARN))
    assert statuses(report) == {"m5.spam": "passed", "m4.warn": "passed"}, _report(report)
    assert world.pending_side_effects == 0
    (latency,) = [e for e in report.results[0].evidence if e["step"] == "广告处理耗时"]
    assert latency["seconds"] > config.reply_timeout  # kept as evidence of the delay
    chat = world.chats[_group_chat(world)]
    assert chat.restricted == set()
    assert chat.members[MEMBER.id] == "member"


async def test_m5_verdict_beyond_budget_fails_without_leaking(
    world: FakeTelegram, config: E2EConfig
) -> None:
    # The LLM gives up (fail open): the ad stays. m5.spam fails, nothing happens
    # later by the bot's own budget, and the next scenario runs normally.
    assert world.bot is not None
    world.bot.antispam_times_out = True
    report = await run(world, config, scenarios=select_scenarios(SPAM_THEN_WARN))
    assert statuses(report) == {"m5.spam": "failed", "m4.warn": "passed"}, _report(report)
    assert "没有被删除" in report.results[0].detail
    steps = [e["step"] for e in report.results[0].evidence]
    assert any("没有处理广告" in step for step in steps)


async def test_m5_unconfirmed_cleanup_stops_the_run(world: FakeTelegram, config: E2EConfig) -> None:
    # The history purge never shows up: m5.spam cannot prove its side effects are
    # over, so it fails and later scenarios are not run (no success can be recorded).
    assert world.bot is not None
    world.bot.antispam_no_purge = True
    report = await run(world, config, scenarios=select_scenarios(SPAM_THEN_WARN))
    assert statuses(report) == {"m5.spam": "failed", "m4.warn": "skipped"}, _report(report)
    spam, warn = report.results
    assert "未确认结束" in spam.detail
    assert "没有被删除" in spam.detail  # the original failure is kept
    assert warn.skip_reason == "aborted"
    assert "异步副作用未确认结束" in warn.detail
    assert report.status == "failed"
    assert world.chats[_group_chat(world)].restricted == set()  # still unmuted


async def test_restore_member_lifts_a_mute(world: FakeTelegram) -> None:
    # A muted member is still in the group (RC2: a late spam mute during m4.warn).
    # Restoring lifts the mute instead of trying to add the member again.
    chat = world.add_chat("AkkiaBot E2E 自动验收群", ADMIN)
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.restricted.add(MEMBER.id)
    ctx = ScenarioContext(
        admin=world.account(ADMIN),
        member=world.account(MEMBER),
        admin_user=ADMIN,
        member_user=MEMBER,
        bot=BOT,
        chat=ChatRef(chat.id, chat.title),
        run_id="test-run",
    )
    await restore_member(ctx)
    assert chat.restricted == set()
    assert chat.members[MEMBER.id] == "member"
    assert "invite" not in world.calls


async def test_failed_restore_keeps_the_scenario_failure(world: FakeTelegram) -> None:
    # The run stops when the member cannot be added back, but the report still
    # says why the scenario itself failed first.
    assert world.bot is not None
    world.bot.no_purge = True
    chat = world.add_chat("AkkiaBot E2E 自动验收群", ADMIN)
    chat.members.update({BOT.id: "admin", MEMBER.id: "member"})
    chat.rights[BOT.id] = BOT_ADMIN_RIGHTS
    world.invite_errors[MEMBER.id] = "USER_PRIVACY_RESTRICTED"
    config = load_config(live_env(E2E_ALLOWED_CHAT_IDS=str(chat.id)))
    report = await run(
        world,
        config,
        allow_create=False,
        scenarios=select_scenarios(["m4.ban", "m5.spam-command"]),
    )
    ban, spam_command = report.results
    assert ban.status == "failed", _report(report)
    assert "恢复成员失败" in ban.detail
    assert "只删除了 0 条消息" in ban.detail
    assert spam_command.skip_reason == "aborted"
