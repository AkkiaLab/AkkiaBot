"""m7.captcha-test-keys: the local Turnstile test-key chain against the fake bot."""

from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Literal

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.config import E2EConfig
from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.pacing import Pacer
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.report import Report
from akkiabot_e2e.runner import run_acceptance
from akkiabot_e2e.scenarios import (
    LOCAL_SCENARIOS,
    SCENARIOS,
    TEST_KEYS_NOTE,
    TEST_KEYS_SCENARIO,
    MiniAppVisit,
    catalog,
    select_scenarios,
)
from akkiabot_e2e.testing import (
    MEMBER,
    Connector,
    FakeRegistry,
    FakeTelegram,
    FakeTraces,
    live_env,
)
from akkiabot_e2e.turnstile_lab import PlaywrightMiniApp

FAIL_ORIGIN = "http://127.0.0.1:2"


async def _no_sleep(_: float) -> None:
    return None


@dataclass
class FakeLab:
    """Local Mini App builds + bot restart, answering like the real page would."""

    world: FakeTelegram
    widget_fail_origin: str = FAIL_ORIGIN
    siteverify: Literal["pass", "fail"] = "fail"
    # -- regressions the scenario must catch --
    unmute_on_reject: bool = False
    widget_error_submits: bool = False
    no_close: bool = False
    restarts: list[str] = field(default_factory=list)
    visits: list[str] = field(default_factory=list)

    pass_origin: str = field(default="", init=False)

    def __post_init__(self) -> None:
        assert self.world.bot is not None
        self.pass_origin = self.world.bot.webapp_origin

    def describe(self) -> list[str]:
        return ["fake test keys"]

    async def use_secret(self, which: Literal["pass", "fail"]) -> None:
        self.restarts.append(which)
        self.siteverify = which

    def _unmute(self, url: str) -> None:
        assert self.world.bot is not None
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        target = self.world.bot.challenge_of(query["challenge"][0])
        assert target is not None
        self.world.chats[target[0]].restricted.discard(target[1])

    async def visit(self, url: str) -> MiniAppVisit:
        self.visits.append(url)
        assert "#tgWebAppData=" in url, "the visit must carry Telegram's launch parameters"
        ready = ("iframe_ready", "web_app_ready")
        if url.startswith(self.widget_fail_origin):
            if self.widget_error_submits:
                return MiniAppVisit(
                    "failed", "人机验证未通过，请重试。", ((400, "turnstile_failed"),)
                )
            return MiniAppVisit("failed", "人机验证组件加载失败，请重试。", (), ready)
        if self.siteverify == "fail":
            if self.unmute_on_reject:
                self._unmute(url)
            return MiniAppVisit(
                "failed", "人机验证未通过，请重试。", ((400, "turnstile_failed"),), ready
            )
        self._unmute(url)
        events = ready if self.no_close else (*ready, "web_app_close")
        return MiniAppVisit("passed", "验证成功，你已被解除禁言。", ((200, None),), events)


async def run_test_keys(world: FakeTelegram, config: E2EConfig, lab: FakeLab | None) -> Report:
    return await run_acceptance(
        config,
        select_scenarios([TEST_KEYS_SCENARIO], serve=True),
        connect=Connector(world),
        run_id="test-run",
        allow_create=True,
        pacer=Pacer(send_interval=0, sleep=_no_sleep),
        poll_interval=0.01,
        traces=FakeTraces(),
        registry=FakeRegistry(),
        catalog=catalog(serve=True),
        turnstile_lab=lab,
    )


def _result(report: Report) -> tuple[str, str]:
    (result,) = [r for r in report.results if r.name == TEST_KEYS_SCENARIO]
    return result.status, result.detail


def assert_cleaned_up(world: FakeTelegram) -> None:
    (chat_id,) = [c for c in world.chats if c < 0]
    chat = world.chats[chat_id]
    assert chat.members[MEMBER.id] == "member"
    assert MEMBER.id not in chat.restricted
    assert world.bot is not None
    assert world.bot.settings[chat.id]["captcha"] is False


async def test_widget_failure_siteverify_rejection_then_pass(
    world: FakeTelegram, config: E2EConfig
) -> None:
    lab = FakeLab(world)
    report = await run_test_keys(world, config, lab)

    assert report.status == "passed", report.to_markdown(Redactor())
    assert lab.restarts == ["pass"]  # starts on the always-fail secret, restarts once
    origins = [u.split("/captcha", 1)[0] for u in lab.visits]
    assert origins == [FAIL_ORIGIN, lab.pass_origin, lab.pass_origin]
    # The scenario's join is the owner adding the member: no rate-limited link join
    # (the other add is the group preparation of the fake world).
    assert report.joins == {"member_join_by_link": 0, "owner_added_member": 2}
    # Reported as its own class of evidence, separate from real-challenge evidence.
    assert report.test_keys == [TEST_KEYS_NOTE, "fake test keys"]
    assert report.not_automated == []
    markdown = report.to_markdown(Redactor())
    assert "官方测试 key（不代表真实挑战通过）" in markdown
    steps = [e["step"] for r in report.results for e in r.evidence]
    assert "widget 失败后检查成员状态" in steps
    assert "Siteverify 拒绝后检查成员状态" in steps
    assert "成员已被解除禁言" in steps
    assert "fake-hash" not in str(report.results[0].evidence)  # initData stays out
    assert_cleaned_up(world)


@pytest.mark.parametrize(
    ("knob", "message"),
    [
        ("unmute_on_reject", "Siteverify 拒绝，成员却被解除了禁言"),
        ("widget_error_submits", "固定失败的 widget"),
        ("no_close", "没有关闭 Mini App"),
    ],
)
async def test_regressions_fail(
    world: FakeTelegram, config: E2EConfig, knob: str, message: str
) -> None:
    lab = FakeLab(world, **{knob: True})  # type: ignore[arg-type]
    report = await run_test_keys(world, config, lab)
    status, detail = _result(report)
    assert status == "failed"
    assert message in detail
    assert report.status == "failed"
    assert_cleaned_up(world)


async def test_member_not_unmuted_fails(world: FakeTelegram, config: E2EConfig) -> None:
    class NoUnmute(FakeLab):
        def _unmute(self, url: str) -> None:
            return None

    report = await run_test_keys(world, config, NoUnmute(world))
    status, detail = _result(report)
    assert status == "failed"
    assert "仍被禁言" in detail


async def test_skipped_without_local_lab(world: FakeTelegram, config: E2EConfig) -> None:
    report = await run_test_keys(world, config, None)
    status, detail = _result(report)
    assert status == "skipped"
    assert "--serve" in detail
    assert report.status == "failed"  # wanted but not verified: never a pass


def test_local_scenarios_only_by_exact_name_and_with_serve() -> None:
    names = [s.name for s in select_scenarios(None, serve=True)]
    assert TEST_KEYS_SCENARIO not in names
    assert TEST_KEYS_SCENARIO not in [s.name for s in select_scenarios(["m7"], serve=True)]
    assert [s.name for s in select_scenarios([TEST_KEYS_SCENARIO], serve=True)] == [
        TEST_KEYS_SCENARIO
    ]
    with pytest.raises(ValueError, match="--serve"):
        select_scenarios([TEST_KEYS_SCENARIO])
    # The staging release catalog (full run, --record-status) never contains it.
    assert catalog(serve=False) == SCENARIOS
    assert catalog(serve=True) == SCENARIOS + LOCAL_SCENARIOS


def test_cli_rejects_test_keys_scenario_without_serve(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "--scenarios", TEST_KEYS_SCENARIO], env=live_env())
    assert code == cli.EXIT_USAGE
    assert "--serve" in capsys.readouterr().err


def test_cli_lists_local_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"], env={}) == 0
    assert TEST_KEYS_SCENARIO in capsys.readouterr().out


# mock: stand-in for the signed initData in Telegram's launch URL; never a real value.
MOCK_INIT_DATA_MARKER = "mock-init-data-marker"


class _NavigationFails:
    """Playwright objects whose navigation error quotes the full URL, like the real one."""

    async def new_context(self) -> _NavigationFails:
        return self

    async def add_init_script(self, script: str) -> None:
        return None

    async def new_page(self) -> _NavigationFails:
        return self

    def on(self, event: str, handler: object) -> None:
        return None

    async def goto(self, url: str) -> None:
        raise RuntimeError(f"Page.goto: net::ERR_CONNECTION_REFUSED at {url}")

    async def close(self) -> None:
        return None


async def test_browser_errors_never_leak_the_launch_url(
    world: FakeTelegram, config: E2EConfig, caplog: pytest.LogCaptureFixture
) -> None:
    browser = PlaywrightMiniApp()
    browser._browser = _NavigationFails()  # type: ignore[assignment]  # pyright: ignore[reportAttributeAccessIssue]

    class LeakyLab(FakeLab):
        async def visit(self, url: str) -> MiniAppVisit:
            marked = url.replace("tgWebAppData=", f"tgWebAppData={MOCK_INIT_DATA_MARKER}", 1)
            return await browser.visit(marked)

    # The browser boundary alone: no detail, no exception chain.
    with pytest.raises(E2EError) as raised:
        await browser.visit(f"http://127.0.0.1:9/captcha#tgWebAppData={MOCK_INIT_DATA_MARKER}")
    assert MOCK_INIT_DATA_MARKER not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "RuntimeError" in str(raised.value)

    # Through the runner: logs (with tracebacks) and both report formats.
    caplog.set_level(logging.DEBUG)
    report = await run_test_keys(world, config, LeakyLab(world))
    status, _ = _result(report)
    assert status in {"failed", "error"}
    logged = "\n".join(
        r.getMessage() + (logging.Formatter().formatException(r.exc_info) if r.exc_info else "")
        for r in caplog.records
    )
    for text in (report.to_json(Redactor()), report.to_markdown(Redactor()), logged):
        assert MOCK_INIT_DATA_MARKER not in text
        assert "tgWebAppData" not in text
    assert_cleaned_up(world)
