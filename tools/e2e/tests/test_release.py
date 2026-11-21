"""Staging release record: commit status written after a full staging run."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.redaction import Redactor
from akkiabot_e2e.release import (
    STATUS_CONTEXT,
    check_baseline,
    check_sha,
    deployed_webapp_sha,
    describe,
    guarded_run,
    record_status,
)
from akkiabot_e2e.report import Report, ScenarioResult
from akkiabot_e2e.scenarios import HttpResult
from akkiabot_e2e.testing import live_env

SHA = "0123456789abcdef0123456789abcdef01234567"


SHA_B = "fedcba9876543210fedcba9876543210fedcba98"


def _report(
    *statuses: str, not_automated: list[str] | None = None, verified: str | None = None
) -> Report:
    report = Report(run_id="run-1", status="failed", started_at="now")
    if verified is not None:
        report.release = {
            "target_sha": verified,
            "webapp_sha_at_start": verified,
            "webapp_sha_at_end": verified,
        }
    report.results = [ScenarioResult(f"s{i}", "x", "d", status=s) for i, s in enumerate(statuses)]  # type: ignore[arg-type]
    report.not_automated = not_automated or []
    report.finish()
    return report


def test_describe_success_mentions_not_automated_steps() -> None:
    state, text = describe(_report("passed", "passed", not_automated=["turnstile"]))
    assert state == "success"
    assert text == "2/2 个场景通过（运行 run-1）；1 项未自动化，见报告"


def test_describe_failure() -> None:
    state, text = describe(_report("passed", "skipped"))
    assert (state, text) == ("failure", "1/2 个场景通过（运行 run-1）；1 个跳过（不计为通过）")


def test_record_gate_requires_every_scenario_executed() -> None:
    # A PR-level run passes with unselected scenarios, but can never be recorded
    # as staging acceptance: skipped never counts as passed.
    report = Report(run_id="run-1", status="failed", started_at="now")
    report.release = dict.fromkeys(("target_sha", "webapp_sha_at_start", "webapp_sha_at_end"), SHA)
    report.results = [
        ScenarioResult("s0", "moderation", "d", status="passed"),
        ScenarioResult("s1", "commands", "d", status="skipped", skip_reason="not_selected"),
    ]
    report.finish()
    assert report.status == "passed"
    assert not report.fully_executed()
    state, text = describe(report, SHA)
    assert state == "failure"
    assert "1 个跳过（不计为通过）" in text


def test_record_status_posts_through_gh_stdin() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    state = record_status(SHA, _report("passed", verified=SHA), repo="o/r", run=fake_run)

    assert state == "success"
    ((argv, kwargs),) = calls
    assert argv == ["gh", "api", "-X", "POST", f"repos/o/r/statuses/{SHA}", "--input", "-"]
    assert json.loads(kwargs["input"]) == {
        "state": "success",
        "context": STATUS_CONTEXT,
        "description": "1/1 个场景通过（运行 run-1）",
    }


def test_record_status_reports_gh_failure() -> None:
    def failing(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 403")

    with pytest.raises(E2EError, match="退出码 1"):
        record_status(SHA, _report("passed"), run=failing)


@pytest.mark.parametrize("bad", ["", "abc", SHA.upper(), SHA + "0", "master"])
def test_check_sha_rejects_non_commit_ids(bad: str) -> None:
    with pytest.raises(E2EError):
        check_sha(bad)


class _Web:
    def __init__(self, result: HttpResult) -> None:
        self.result = result
        self.urls: list[str] = []

    async def get(self, url: str) -> HttpResult:
        self.urls.append(url)
        return self.result

    async def post_json(self, url: str, body: Any, *, origin: str) -> HttpResult:
        raise AssertionError("not used")


async def test_deployed_webapp_sha_reads_version_json() -> None:
    web = _Web(HttpResult(200, json.dumps({"sha": SHA, "environment": "staging"})))
    assert await deployed_webapp_sha(web, "https://s.example") == SHA
    assert web.urls == ["https://s.example/version.json"]


@pytest.mark.parametrize("result", [HttpResult(404, "nope"), HttpResult(200, "<html>")])
async def test_deployed_webapp_sha_unknown(result: HttpResult) -> None:
    assert await deployed_webapp_sha(_Web(result), "https://s.example") is None


def test_record_status_never_with_local_server(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "--record-status", SHA, "--serve"], env=live_env())
    assert code == cli.EXIT_USAGE
    assert "不能与 --serve 同用" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [["--scenarios", "captcha"], ["--baseline", SHA_B]],
    ids=["scenarios-without-baseline", "baseline-without-scenarios"],
)
def test_diff_record_needs_both_scenarios_and_baseline(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", "--record-status", SHA, *extra], env=live_env())
    assert code == cli.EXIT_USAGE
    assert "同时给出 --scenarios 和 --baseline" in capsys.readouterr().err


def test_baseline_only_with_record_status(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "--scenarios", "commands", "--baseline", SHA_B], env=live_env())
    assert code == cli.EXIT_USAGE
    assert "需与 --record-status 同用" in capsys.readouterr().err


def test_record_status_requires_a_full_sha(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "--record-status", "HEAD"], env=live_env()) == cli.EXIT_USAGE
    assert "40 位" in capsys.readouterr().err


def _env_file(path: str) -> dict[str, str]:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    pairs: dict[str, str] = {}
    for line in (root / path).read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
    return pairs


def test_staging_release_template_targets_the_deployed_staging_stack() -> None:
    from akkiabot_e2e.config import DEFAULT_CHAT_TITLE

    release = _env_file("tools/e2e/staging-release.env.tpl")
    staging_app = _env_file("deploy/staging/app.env.tpl")
    local = _env_file("tools/e2e/staging.env.tpl")
    # The staging bot itself, not the e2e bot that `just e2e --serve` runs.
    assert release["TELEGRAM_BOT_TOKEN"] == staging_app["TELEGRAM_BOT_TOKEN"]
    assert release["E2E_BOT_USERNAME"] == "op://AkkiaBot/staging-bot/bot_username"
    assert release["E2E_BOT_USERNAME"] != local["E2E_BOT_USERNAME"]
    # The Mini App the staging bot links to, and a group of its own.
    assert release["E2E_WEBAPP_URL"] == staging_app["WEBAPP_URL"]
    assert release["E2E_API_BASE_URL"] == "https://bot-api-staging.akkia.moe"
    assert release["E2E_CHAT_TITLE"] not in {DEFAULT_CHAT_TITLE, local.get("E2E_CHAT_TITLE")}


class _Deployments:
    """Serves /version.json; ``versions`` are returned in order (last one repeats)."""

    def __init__(self, *versions: str | None) -> None:
        self.versions = list(versions)

    async def get(self, url: str) -> HttpResult:
        sha = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        if sha is None:
            return HttpResult(404, "not found")
        return HttpResult(200, json.dumps({"sha": sha, "environment": "staging"}))

    async def post_json(self, url: str, body: Any, *, origin: str) -> HttpResult:
        raise AssertionError("not used")


def _passing_run(calls: list[str]):
    async def run() -> Report:
        calls.append("run")
        return _report("passed", "passed")

    return run


async def test_guarded_run_records_the_verified_version() -> None:
    calls: list[str] = []
    report = await guarded_run(
        SHA, "https://s.example", _Deployments(SHA, SHA), _passing_run(calls)
    )

    assert calls == ["run"]
    assert report.status == "passed"
    assert report.release == {
        "target_sha": SHA,
        "baseline_sha": None,
        "webapp_sha_at_start": SHA,
        "webapp_sha_at_end": SHA,
    }
    assert describe(report, SHA)[0] == "success"
    assert f"结束时为 `{SHA}`" in report.to_markdown(Redactor())


@pytest.mark.parametrize("end", [SHA_B, None], ids=["replaced-by-B", "unknown"])
async def test_guarded_run_fails_when_staging_changes_during_the_run(end: str | None) -> None:
    """A→B: a master push replaced the staging alias while A was being verified."""
    report = await guarded_run(SHA, "https://s.example", _Deployments(SHA, end), _passing_run([]))

    assert report.status == "failed"
    assert "验收期间 staging Mini App" in report.detail
    assert report.release is not None
    assert report.release["webapp_sha_at_end"] == end
    state, text = describe(report, SHA)
    assert state == "failure"
    assert "版本未保持一致" in text


async def test_guarded_run_refuses_to_start_on_another_version() -> None:
    calls: list[str] = []
    with pytest.raises(E2EError, match="不是要验收的"):
        await guarded_run(SHA, "https://s.example", _Deployments(SHA_B), _passing_run(calls))
    assert calls == []  # nothing was sent to Telegram


def test_success_requires_the_version_check() -> None:
    passed = _report("passed")  # no guarded_run record at all
    assert describe(passed)[0] == "success"  # plain report summary
    assert describe(passed, SHA)[0] == "failure"
    assert describe(_report("passed", verified=SHA_B), SHA)[0] == "failure"
    assert describe(_report("passed", verified=SHA), SHA)[0] == "success"


def test_record_status_never_writes_success_for_an_unverified_report() -> None:
    sent: list[str] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.append(json.loads(kwargs["input"])["state"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert record_status(SHA, _report("passed", verified=SHA_B), run=fake_run) == "failure"
    assert sent == ["failure"]


# -- diff release runs (--record-status --scenarios --baseline) --------------


def _diff_report(*statuses: str, verified: str | None = SHA, baseline: str = SHA_B) -> Report:
    """``statuses`` of the selected scenarios; two more are not selected."""
    report = Report(run_id="run-2", status="failed", started_at="now")
    if verified is not None:
        report.release = {
            "target_sha": verified,
            "baseline_sha": baseline,
            "webapp_sha_at_start": verified,
            "webapp_sha_at_end": verified,
        }
    report.results = [
        ScenarioResult(f"commands.s{i}", "commands", "d", status=s)  # type: ignore[arg-type]
        for i, s in enumerate(statuses)
    ]
    report.results += [
        ScenarioResult(
            f"antispam.u{i}", "antispam", "d", status="skipped", skip_reason="not_selected"
        )
        for i in range(2)
    ]
    report.finish()
    return report


def test_diff_record_success_names_baseline_and_scenarios() -> None:
    state, text = describe(_diff_report("passed"), SHA)
    assert state == "success"
    assert text == (
        f"差异验收（基线 {SHA_B[:7]}）：1/1 个选中场景通过（运行 run-2）；"
        "2 个未涉及差异、未执行；场景 commands.s0"
    )


@pytest.mark.parametrize("selected", [("passed", "failed"), ("passed", "error")])
def test_diff_record_fails_when_a_selected_scenario_fails(selected: tuple[str, ...]) -> None:
    state, text = describe(_diff_report(*selected), SHA)
    assert state == "failure"
    assert text.startswith(f"差异验收（基线 {SHA_B[:7]}）：1/2 个选中场景通过")


def test_diff_record_fails_when_a_selected_scenario_is_skipped() -> None:
    report = _diff_report("passed")
    report.results[0] = ScenarioResult(
        "commands.s0", "commands", "d", status="skipped", skip_reason="rate_limited"
    )
    report.finish()
    state, text = describe(report, SHA)
    assert state == "failure"
    assert "0/1 个选中场景通过" in text
    assert "1 个选中场景跳过（不计为通过）" in text


def test_diff_record_still_requires_the_version_check() -> None:
    report = _diff_report("passed", verified=SHA_B, baseline="1" * 40)
    state, text = describe(report, SHA)
    assert state == "failure"
    assert "版本未保持一致" in text


def test_diff_record_description_fits_github_limit() -> None:
    report = _diff_report(*["passed"] * 20)
    state, text = describe(report, SHA)
    assert state == "success"
    assert len(text) <= 140
    assert text.endswith("…")


def test_diff_report_markdown_lists_baseline_and_unrun_scenarios() -> None:
    md = _diff_report("passed").to_markdown(Redactor())
    assert f"差异验收：基线 `{SHA_B}`" in md
    assert "执行场景 `commands.s0`" in md
    assert "其余 2 个场景未涉及基线以来的改动" in md
    assert "未选中（本次改动未涉及）" in md


def test_full_report_markdown_says_full() -> None:
    md = _report("passed", verified=SHA).to_markdown(Redactor())
    assert "全量验收" in md


def test_record_status_writes_diff_description() -> None:
    sent: list[dict[str, str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert record_status(SHA, _diff_report("passed"), run=fake_run) == "success"
    assert record_status(SHA, _diff_report("failed"), run=fake_run) == "failure"
    assert [p["state"] for p in sent] == ["success", "failure"]
    assert all(f"基线 {SHA_B[:7]}" in p["description"] for p in sent)


async def test_guarded_run_records_the_baseline() -> None:
    report = await guarded_run(
        SHA, "https://s.example", _Deployments(SHA, SHA), _passing_run([]), baseline=SHA_B
    )
    assert report.release is not None
    assert report.release["baseline_sha"] == SHA_B


class _Gh:
    """Fake ``gh api`` answering the baseline checks."""

    def __init__(self, state: str | None = "success", compare: str = "ahead", code: int = 0):
        self.state, self.compare, self.code = state, compare, code
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[-1].endswith("/status"):
            statuses = [{"context": "other", "state": "success"}]
            if self.state is not None:
                statuses.append({"context": STATUS_CONTEXT, "state": self.state})
            body: dict[str, Any] = {"statuses": statuses}
        else:
            body = {"status": self.compare}
        return subprocess.CompletedProcess(argv, self.code, json.dumps(body), "")


def test_check_baseline_accepts_an_accepted_ancestor() -> None:
    gh = _Gh()
    check_baseline(SHA_B, SHA, repo="o/r", run=gh)
    assert gh.calls == [
        ["gh", "api", f"repos/o/r/commits/{SHA_B}/status"],
        ["gh", "api", f"repos/o/r/compare/{SHA_B}...{SHA}"],
    ]


@pytest.mark.parametrize(
    ("gh", "match"),
    [
        (_Gh(state="failure"), "不是 success"),
        (_Gh(state=None), "未写入"),
        (_Gh(compare="behind"), "不是 ahead"),
        (_Gh(compare="diverged"), "不是 ahead"),
        (_Gh(code=1), "gh 退出码 1"),
    ],
    ids=["baseline-failed", "baseline-unaccepted", "behind", "diverged", "gh-error"],
)
def test_check_baseline_rejects(gh: _Gh, match: str) -> None:
    with pytest.raises(E2EError, match=match):
        check_baseline(SHA_B, SHA, run=gh)


def test_check_baseline_rejects_the_target_itself() -> None:
    with pytest.raises(E2EError, match="不能等于"):
        check_baseline(SHA, SHA, run=_Gh())


def _diff_cli(
    tmp_path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> tuple[int, dict[str, Any]]:
    """Drive ``run --record-status --scenarios --baseline`` with fakes for Telegram and gh."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen: dict[str, Any] = {}

    async def fake_run_acceptance(config, scenarios, **kwargs: Any) -> Report:
        seen["scenarios"] = [s.name for s in scenarios]
        seen["fresh_group"] = kwargs["fresh_group"]
        report = Report(run_id=kwargs["run_id"], status="failed", started_at="-")
        report.results = [
            ScenarioResult(s.name, s.group, "d", status=outcome)  # type: ignore[arg-type]
            for s in scenarios
        ]
        report.results.append(
            ScenarioResult(
                "antispam.enabled", "antispam", "d", status="skipped", skip_reason="not_selected"
            )
        )
        report.finish()
        return report

    async def fake_check(token, username, http) -> None:
        return None

    def fake_baseline(baseline: str, sha: str, *, repo: str) -> None:
        seen["baseline_checked"] = (baseline, sha, repo)

    def fake_record(sha: str, report: Report, *, repo: str) -> str:
        seen["status"] = describe(report, sha)
        return seen["status"][0]

    monkeypatch.setattr(cli, "run_acceptance", fake_run_acceptance)

    class _Web(_Deployments):
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(cli, "HttpWebProbe", lambda: _Web(SHA))
    monkeypatch.setattr(cli, "check_baseline", fake_baseline)
    monkeypatch.setattr(cli, "record_status", fake_record)
    monkeypatch.setattr(cli, "_vault", lambda: None)
    monkeypatch.setattr("akkiabot_e2e.bot_api.check_test_bot", fake_check)
    env = live_env(E2E_WEBAPP_URL="https://webapp.example.test")
    argv = [
        "run",
        "--record-status",
        SHA,
        "--baseline",
        SHA_B,
        "--scenarios",
        "members.join-request",
    ]
    code = cli.main([*argv, "--report-dir", str(tmp_path / "r")], env=env)
    return code, seen


def test_cli_diff_record_success(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, seen = _diff_cli(tmp_path, monkeypatch, "passed")
    assert code == cli.EXIT_PASSED
    assert seen["scenarios"] == ["members.join-request"]
    assert seen["fresh_group"] is True
    assert seen["baseline_checked"] == (SHA_B, SHA, "AkkiaLab/AkkiaBot")
    state, text = seen["status"]
    assert state == "success"
    assert f"基线 {SHA_B[:7]}" in text
    assert "场景 members.join-request" in text
    (md,) = (tmp_path / "r").glob("*/report.md")
    assert f"差异验收：基线 `{SHA_B}`" in md.read_text(encoding="utf-8")


def test_cli_diff_record_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, seen = _diff_cli(tmp_path, monkeypatch, "failed")
    assert code == cli.EXIT_FAILED
    assert seen["status"][0] == "failure"


def test_diff_record_never_selects_local_only_scenarios(
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["run", "--record-status", SHA, "--baseline", SHA_B]
    code = cli.main([*argv, "--scenarios", "captcha.test-keys"], env=live_env())
    assert code == cli.EXIT_USAGE
    assert "只能与 --serve 一起使用" in capsys.readouterr().err
