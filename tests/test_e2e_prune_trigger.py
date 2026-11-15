"""Weekly prune trigger: call the Multica webhook only after a staging release (BOT-30)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.e2e_prune_trigger import iso_week, recent_releases, run

NOW = datetime(2026, 11, 16, 3, 17, tzinfo=UTC)
REPO = "AkkiaLab/AkkiaBot"
# mock: 人工构造的不可用 webhook 地址，不是凭据。
MOCK_WEBHOOK_URL = "https://multica.example.test/api/webhooks/autopilots/fake-token-for-tests"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "e2e-prune-trigger.yml"


class FakeGitHub:
    """GraphQL master history, newest first; each commit has its newest status per context."""

    def __init__(self, commits: list[tuple[str, list[dict[str, str]]]]) -> None:
        self.commits = commits
        self.pages: list[str | None] = []
        self.errors: list[dict[str, str]] | None = None

    def __call__(self, query: str, variables: Mapping[str, Any]) -> Any:
        assert "refs/heads/master" in query
        if self.errors:
            return {"data": None, "errors": self.errors}
        after = variables["after"]
        self.pages.append(after)
        start = int(after) if after else 0
        chunk = self.commits[start : start + variables["first"]]
        end = start + len(chunk)
        return {
            "data": {
                "repository": {
                    "ref": {
                        "target": {
                            "history": {
                                "pageInfo": {
                                    "hasNextPage": end < len(self.commits),
                                    "endCursor": str(end),
                                },
                                "nodes": [
                                    {"oid": sha, "status": {"contexts": ctx} if ctx else None}
                                    for sha, ctx in chunk
                                ],
                            }
                        }
                    }
                }
            }
        }


def filler(n: int) -> list[tuple[str, list[dict[str, str]]]]:
    """``n`` newer commits without a staging-acceptance status."""
    return [(f"{i:040x}", []) for i in range(n)]


class FakePost:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        self.calls.append((url, json.loads(body), dict(headers)))
        return self.status


def env(**extra: str) -> dict[str, str]:
    return {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "42", **extra}


def status(context: str, created_at: str, state: str = "SUCCESS") -> dict[str, str]:
    return {"context": context, "createdAt": created_at, "state": state}


def test_no_release_ends_without_calling_the_webhook() -> None:
    gh = FakeGitHub(
        [
            ("a" * 40, [status("staging-acceptance", "2026-11-08T03:16:00Z")]),  # just outside
            ("b" * 40, [status("ci", "2026-11-15T00:00:00Z")]),
            *filler(150),
        ]
    )
    post = FakePost()
    lines: list[str] = []
    # No webhook URL needed when there is nothing to trigger.
    assert run(env(), gh, post, NOW, lines.append) == 0
    assert post.calls == []
    assert "不触发清理" in lines[-1]


def test_release_triggers_the_webhook_once_per_week() -> None:
    gh = FakeGitHub(
        [
            ("a" * 40, [status("staging-acceptance", "2026-11-14T22:00:00Z", "FAILURE")]),
            ("c" * 40, [status("staging-acceptance", "2026-11-15T01:20:00Z")]),
        ]
    )
    post = FakePost()
    lines: list[str] = []
    assert run(env(MULTICA_PRUNE_WEBHOOK_URL=MOCK_WEBHOOK_URL), gh, post, NOW, lines.append) == 0
    ((url, body, headers),) = post.calls
    assert url == MOCK_WEBHOOK_URL
    assert headers["Idempotency-Key"] == "akkiabot-e2e-prune-2026-W47"
    assert [(r["sha"], r["state"]) for r in body["releases"]] == [
        ("c" * 40, "success"),
        ("a" * 40, "failure"),
    ]
    assert body["workflow_run"] == f"https://github.com/{REPO}/actions/runs/42"
    # The webhook URL (a secret) is never printed.
    assert all("fake-token-for-tests" not in line for line in lines)


def test_release_on_a_later_page_is_found() -> None:
    """The accepted commit was pushed down by 250 newer commits (third page)."""
    gh = FakeGitHub(
        [*filler(250), ("d" * 40, [status("staging-acceptance", "2026-11-15T01:20:00Z")])]
    )
    releases = recent_releases(gh, REPO, NOW)
    assert [r.sha for r in releases] == ["d" * 40]
    assert gh.pages == [None, "100", "200"]


def test_old_commit_accepted_this_week_is_found() -> None:
    """No age cut-off on commits: re-accepting an old master commit counts."""
    gh = FakeGitHub(
        [*filler(5), ("e" * 40, [status("staging-acceptance", "2026-11-13T00:00:00Z")])]
    )
    assert [r.sha for r in recent_releases(gh, REPO, NOW)] == ["e" * 40]


def test_incomplete_scan_fails_instead_of_reporting_no_release() -> None:
    post = FakePost()
    lines: list[str] = []
    gh = FakeGitHub(filler(3))
    gh.errors = [{"message": "API rate limit exceeded"}]
    assert run(env(MULTICA_PRUNE_WEBHOOK_URL=MOCK_WEBHOOK_URL), gh, post, NOW, lines.append) == 1
    assert "无法完整判断" in lines[-1]
    assert "rate limit" in lines[-1]

    from scripts.e2e_prune_trigger import IncompleteScan

    with pytest.raises(IncompleteScan, match="未能检查完整"):
        recent_releases(FakeGitHub(filler(250)), REPO, NOW, max_pages=2)
    assert post.calls == []


def test_missing_secret_or_failed_call_fails_the_job() -> None:
    gh = FakeGitHub([("c" * 40, [status("staging-acceptance", "2026-11-15T01:20:00Z")])])
    lines: list[str] = []
    assert run(env(), gh, FakePost(), NOW, lines.append) == 1
    assert "MULTICA_PRUNE_WEBHOOK_URL" in lines[-1]
    lines.clear()
    assert (
        run(env(MULTICA_PRUNE_WEBHOOK_URL=MOCK_WEBHOOK_URL), gh, FakePost(404), NOW, lines.append)
        == 1
    )
    assert lines[-1] == "::error::调用 Multica webhook 失败：HTTP 404"


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
