"""Staging release record: commit status written after a full staging run."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.errors import E2EError
from akkiabot_e2e.release import (
    STATUS_CONTEXT,
    check_sha,
    deployed_webapp_sha,
    describe,
    record_status,
)
from akkiabot_e2e.report import Report, ScenarioResult
from akkiabot_e2e.scenarios import HttpResult
from akkiabot_e2e.testing import live_env

SHA = "0123456789abcdef0123456789abcdef01234567"


def _report(*statuses: str, not_automated: list[str] | None = None) -> Report:
    report = Report(run_id="run-1", status="failed", started_at="now")
    report.results = [ScenarioResult(f"s{i}", "M7", "d", status=s) for i, s in enumerate(statuses)]  # type: ignore[arg-type]
    report.not_automated = not_automated or []
    report.finish()
    return report


def test_describe_success_mentions_not_automated_steps() -> None:
    state, text = describe(_report("passed", "passed", not_automated=["turnstile"]))
    assert state == "success"
    assert text == "2/2 个场景通过（运行 run-1）；1 项未自动化，见报告"


def test_describe_failure() -> None:
    state, text = describe(_report("passed", "not_run"))
    assert (state, text) == ("failure", "1/2 个场景通过（运行 run-1）")


def test_record_status_posts_through_gh_stdin() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    state = record_status(SHA, _report("passed"), repo="o/r", run=fake_run)

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


@pytest.mark.parametrize(
    "extra", [["--scenarios", "m7"], ["--serve"]], ids=["partial", "local-server"]
)
def test_record_status_only_for_full_staging_runs(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", "--record-status", SHA, *extra], env=live_env())
    assert code == cli.EXIT_USAGE
    assert "只用于针对已部署 staging 的全量验收" in capsys.readouterr().err


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
