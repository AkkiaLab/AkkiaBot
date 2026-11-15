"""Weekly prune trigger: call the Multica webhook only after a staging release (BOT-30)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from scripts.e2e_prune_trigger import iso_week, recent_releases, run

NOW = datetime(2026, 11, 16, 3, 17, tzinfo=UTC)
API = "https://api.github.com"
REPO = "AkkiaLab/AkkiaBot"
# mock: 人工构造的不可用 webhook 地址，不是凭据。
MOCK_WEBHOOK_URL = "https://multica.example.test/api/webhooks/autopilots/fake-token-for-tests"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "e2e-prune-trigger.yml"


class FakeGitHub:
    def __init__(self, statuses: dict[str, list[dict[str, str]]]) -> None:
        self.statuses = statuses
        self.urls: list[str] = []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if "/statuses" in url:
            sha = url.split("/commits/")[1].split("/")[0]
            return self.statuses.get(sha, [])
        return [{"sha": sha} for sha in self.statuses]


class FakePost:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        self.calls.append((url, json.loads(body), dict(headers)))
        return self.status


def env(**extra: str) -> dict[str, str]:
    return {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "42", **extra}


def status(context: str, created_at: str, state: str = "success") -> dict[str, str]:
    return {"context": context, "created_at": created_at, "state": state}


def test_no_release_ends_without_calling_the_webhook() -> None:
    gh = FakeGitHub(
        {
            "a" * 40: [status("staging-acceptance", "2026-11-08T03:16:00Z")],  # just outside
            "b" * 40: [status("ci", "2026-11-15T00:00:00Z")],
        }
    )
    post = FakePost()
    lines: list[str] = []
    # No webhook URL needed when there is nothing to trigger.
    assert run(env(), gh, post, NOW, lines.append) == 0
    assert post.calls == []
    assert "不触发清理" in lines[-1]


def test_release_triggers_the_webhook_once_per_week() -> None:
    gh = FakeGitHub(
        {
            "a" * 40: [status("staging-acceptance", "2026-11-14T22:00:00Z", "failure")],
            "c" * 40: [
                status("staging-acceptance", "2026-11-15T01:20:00Z"),
                status("staging-acceptance", "2026-10-01T00:00:00Z"),
            ],
        }
    )
    post = FakePost()
    lines: list[str] = []
    assert run(env(MULTICA_PRUNE_WEBHOOK_URL=MOCK_WEBHOOK_URL), gh, post, NOW, lines.append) == 0
    ((url, body, headers),) = post.calls
    assert url == MOCK_WEBHOOK_URL
    assert headers["Idempotency-Key"] == "akkiabot-e2e-prune-2026-W47"
    assert [r["sha"] for r in body["releases"]] == ["c" * 40, "a" * 40]
    assert body["workflow_run"] == f"https://github.com/{REPO}/actions/runs/42"
    # The webhook URL (a secret) is never printed.
    assert all("fake-token-for-tests" not in line for line in lines)


def test_missing_secret_or_failed_call_fails_the_job() -> None:
    gh = FakeGitHub({"c" * 40: [status("staging-acceptance", "2026-11-15T01:20:00Z")]})
    lines: list[str] = []
    assert run(env(), gh, FakePost(), NOW, lines.append) == 1
    assert "MULTICA_PRUNE_WEBHOOK_URL" in lines[-1]
    lines.clear()
    assert (
        run(env(MULTICA_PRUNE_WEBHOOK_URL=MOCK_WEBHOOK_URL), gh, FakePost(404), NOW, lines.append)
        == 1
    )
    assert lines[-1] == "::error::调用 Multica webhook 失败：HTTP 404"


def test_queries_master_commits_with_lookback() -> None:
    gh = FakeGitHub({})
    assert recent_releases(gh, API, REPO, NOW) == []
    assert gh.urls == [
        f"{API}/repos/{REPO}/commits?sha=master&since=2026-09-17T03:17:00Z&per_page=100"
    ]


def test_iso_week() -> None:
    assert iso_week(datetime(2027, 1, 1, tzinfo=UTC)) == "2026-W53"


def test_workflow_is_weekly_manual_and_read_only() -> None:
    data = yaml.safe_load(WORKFLOW.read_text())
    triggers = data[True]  # YAML 1.1 parses the `on` key as True
    assert "workflow_dispatch" in triggers
    assert len(triggers["schedule"]) == 1
    assert data["permissions"] == {"contents": "read", "statuses": "read"}
    step = data["jobs"]["check"]["steps"][-1]
    assert step["run"] == "python3 scripts/e2e_prune_trigger.py"
    assert step["env"]["MULTICA_PRUNE_WEBHOOK_URL"] == "${{ secrets.MULTICA_PRUNE_WEBHOOK_URL }}"
