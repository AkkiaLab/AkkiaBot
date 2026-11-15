"""M7: the join-captcha chain against the fake bot / Mini App / API."""

from __future__ import annotations

import pytest

from akkiabot_e2e.config import E2EConfig, load_config
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import TURNSTILE_NOT_AUTOMATED, launch_init_data, select_scenarios
from akkiabot_e2e.testing import (
    MEMBER,
    Connector,
    FakeRegistry,
    FakeTelegram,
    FakeTraces,
    FakeWeb,
    live_env,
)


async def _no_sleep(_: float) -> None:
    return None


async def run_m7(world: FakeTelegram, config: E2EConfig, web: FakeWeb | None = None) -> Report:
    return await run_acceptance(
        config,
        select_scenarios(["m7"]),
        connect=Connector(world),
        run_id="test-run",
        allow_create=True,
        pacer=Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        traces=FakeTraces(),
        registry=FakeRegistry(),
        web=web if web is not None else FakeWeb(world),
    )


def _chat_id(world: FakeTelegram) -> int:
    (chat_id,) = [c for c in world.chats if c < 0]
    return chat_id


def assert_cleaned_up(world: FakeTelegram) -> None:
    chat = world.chats[_chat_id(world)]
    assert chat.members[MEMBER.id] == "member"
    assert MEMBER.id not in chat.restricted
    assert world.bot is not None
    assert world.bot.settings[chat.id]["captcha"] is False


async def test_m7_chain_passes_and_lists_turnstile_as_not_automated(
    world: FakeTelegram, config: E2EConfig
) -> None:
    web = FakeWeb(world)
    report = await run_m7(world, config, web)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert report.not_automated == [TURNSTILE_NOT_AUTOMATED]
    markdown = report.to_markdown(Redactor())
    assert "未自动化（不计入通过）" in markdown
    steps = [e["step"] for e in report.results[0].evidence]
    assert "以未通过 Turnstile 的 token 提交验证" in steps
    assert [method for method, _ in web.requests] == ["GET", "POST"]
    assert_cleaned_up(world)


async def test_m7_evidence_never_contains_init_data(world: FakeTelegram, config: E2EConfig) -> None:
    report = await run_m7(world, config)
    evidence = str(report.results[0].evidence)
    assert "fake-hash" not in evidence
    assert "init_data_length" in evidence


@pytest.mark.parametrize(
    ("knob", "web_kwargs", "message"),
    [
        ("captcha_no_mute", {}, "成员没有被禁言"),
        ("captcha_plain_link", {}, "web_app 按钮"),
        ("captcha_no_mention", {}, "提及链接"),
        (None, {"cors": False}, "CORS"),
        (None, {"accept_any_token": True}, "400 turnstile_failed"),
        (None, {"page_status": 404}, "HTTP 404"),
    ],
)
async def test_m7_regressions_are_caught_and_cleaned_up(
    world: FakeTelegram,
    config: E2EConfig,
    knob: str | None,
    web_kwargs: dict[str, bool | int],
    message: str,
) -> None:
    assert world.bot is not None
    if knob:
        setattr(world.bot, knob, True)
    report = await run_m7(world, config, FakeWeb(world, **web_kwargs))  # type: ignore[arg-type]

    assert report.status == "failed"
    assert report.results[0].status == "failed"
    assert message in report.results[0].detail
    assert report.not_automated == []
    assert_cleaned_up(world)


async def test_m7_wrong_webapp_origin_fails(world: FakeTelegram) -> None:
    config = load_config(live_env(E2E_WEBAPP_URL="https://staging.example.test"))
    report = await run_m7(world, config)
    assert report.results[0].status == "failed"
    assert "不是预期的 https://staging.example.test" in report.results[0].detail


async def test_m7_skipped_without_api(world: FakeTelegram) -> None:
    config = load_config(live_env(E2E_API_BASE_URL=""))
    report = await run_m7(world, config)
    assert report.results[0].status == "skipped"
    assert report.results[0].skip_reason == "missing_credentials"
    assert "E2E_API_BASE_URL" in report.results[0].detail
    assert report.status == "failed"


def test_launch_init_data_reads_the_fragment() -> None:
    url = (
        "https://w.example/captcha?challenge=x#tgWebAppData=user%3D1%26hash%3Dh&tgWebAppVersion=8.0"
    )
    assert launch_init_data(url) == "user=1&hash=h"
    assert launch_init_data("https://w.example/captcha") == ""
