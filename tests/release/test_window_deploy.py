"""Release window orchestration and the deploy gates."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.release import deploy
from scripts.release.branches import create_release
from scripts.release.cascade import run_cascade
from scripts.release.rc import create_rc
from scripts.release.schedule import Phase
from scripts.release.seal import seal
from scripts.release.versions import ReleaseError, Version
from scripts.release.window import RC_WORKFLOW, SEAL_WORKFLOW, run_window
from tests.release.conftest import FakeGitHub, Repo

V = Version.parse
PREPARE = datetime(2026, 11, 19, 8, 30, tzinfo=UTC)  # Thu 16:30 UTC+8
FREEZE = datetime(2026, 11, 19, 10, 5, tzinfo=UTC)  # Thu 18:05
RELEASE = datetime(2026, 11, 19, 12, 5, tzinfo=UTC)  # Thu 20:05
LATE = datetime(2026, 11, 19, 15, 30, tzinfo=UTC)  # Thu 23:30, cron delayed for hours


def test_window_not_bootstrapped_or_outside(repo: Repo, gh: FakeGitHub) -> None:
    assert "未引导" in run_window(repo.git, gh, FREEZE).lines[0]
    create_release(repo.git, V("v1.0.0"))
    assert run_window(repo.git, gh, datetime(2026, 11, 18, tzinfo=UTC)).phase == Phase.NONE


def test_window_without_content_is_skipped(repo: Repo, gh: FakeGitHub) -> None:
    create_release(repo.git, V("v1.0.0"))
    repo.push(repo.sha("refs/heads/releases/v1.0.0"), "refs/tags/v1.0.0")
    gh.sealed.add("releases/v1.0.0")
    repo.commit("master", "m.txt", "m\n")
    create_release(repo.git, V("v1.1.0"))
    # v1.1.0 has master work → content; v1.0.1 (patch) without commits → skip.
    create_release(repo.git, V("v1.0.1"))
    report = run_window(repo.git, gh, RELEASE, version=V("v1.0.1"))
    assert "没有新提交" in report.lines[0]
    assert not report.failed
    assert gh.dispatched == []


def test_full_window_is_idempotent_and_tolerates_delay(repo: Repo, gh: FakeGitHub) -> None:
    create_release(repo.git, V("v1.0.0"))
    repo.commit("releases/v1.0.0", "f.txt", "feature\n")

    prepare = run_window(repo.git, gh, PREPARE)
    assert "releases/v1.1.0" in prepare.lines[0]
    assert V("v1.1.0") in repo.git.state().branches

    freeze = run_window(repo.git, gh, FREEZE)
    assert any("创建 v1.0.0-rc.1" in line for line in freeze.lines)
    again = run_window(repo.git, gh, FREEZE)  # duplicate trigger
    assert any("沿用 v1.0.0-rc.1" in line for line in again.lines)
    assert [d for d in gh.dispatched if d[0] == RC_WORKFLOW] == [
        (RC_WORKFLOW, {"tag": "v1.0.0-rc.1"}),
        (RC_WORKFLOW, {"tag": "v1.0.0-rc.1"}),
    ]

    blocked = run_window(repo.git, gh, RELEASE)
    assert blocked.failed
    assert "staging-acceptance" in blocked.lines[0]
    assert not any(d[0] == SEAL_WORKFLOW for d in gh.dispatched)

    rc_sha = repo.sha("refs/tags/v1.0.0-rc.1")
    gh.statuses[rc_sha] = "success"
    released = run_window(repo.git, gh, LATE)  # schedule ran hours late: same decision
    assert not released.failed
    assert gh.dispatched[-1] == (SEAL_WORKFLOW, {"version": "v1.0.0"})

    seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    # Sealed: v1.1.0 becomes current; its window has content (nothing new yet → skip).
    after = run_window(repo.git, gh, LATE)
    assert after.version == V("v1.1.0")


def test_new_commit_after_rc_stops_the_release(repo: Repo, gh: FakeGitHub) -> None:
    create_release(repo.git, V("v1.0.0"))
    repo.commit("releases/v1.0.0", "f.txt", "feature\n")
    candidate, _ = create_rc(repo.git, gh, V("v1.0.0"))
    gh.statuses[candidate.sha] = "success"
    repo.commit("releases/v1.0.0", "fix.txt", "fix after rc\n")
    report = run_window(repo.git, gh, RELEASE)
    assert report.failed
    assert "已过期" in report.lines[0]


class RecordingRunner:
    def __init__(self, current: str, code: int = 0) -> None:
        self.current = current
        self.code = code
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self, args: list[str], stdin: str | None, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, stdin))
        if stdin is None:
            return subprocess.CompletedProcess(args, 0, self.current + "\n", "")
        return subprocess.CompletedProcess(args, self.code, "upgrade: done\n", "")


def _release_commit(repo: Repo, gh: FakeGitHub, *, migration: bool) -> tuple[str, str]:
    old = repo.sha("refs/heads/master")
    create_release(repo.git, V("v1.0.0"))
    repo.commit("releases/v1.0.0", "deploy/upgrade.sh", "#!/bin/sh\necho upgrade\n")
    if migration:
        repo.commit("releases/v1.0.0", f"{deploy.MIGRATIONS_DIR}/0002_x.py", "x\n")
    candidate, _ = create_rc(repo.git, gh, V("v1.0.0"))
    return old, candidate.sha


def test_staging_deploy_passes_gates_to_upgrade(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy, "template_environment", lambda: {"PATH": "/usr/bin"})
    monkeypatch.setattr(deploy, "http_ok", lambda url: True)
    old, sha = _release_commit(repo, gh, migration=True)
    monkeypatch.setattr(deploy, "fetch_json", lambda url: {"sha": sha})
    runner = RecordingRunner(old)
    result = deploy.deploy(
        repo.git, gh, deploy.TARGETS["staging"], "v1.0.0-rc.1", run=runner, log=lambda _: None
    )
    assert result.code == 0
    args, script = runner.calls[-1]
    assert script is not None
    assert "echo upgrade" in script
    assert args[:2] == ["bash", "-c"]
    assert f"bash -s -- {sha} --migrations changed --release v1.0.0-rc.1" in args[2]
    assert "--render-template staging/app.env.tpl" in args[2]


def test_staging_deploy_refuses_stale_rc(repo: Repo, gh: FakeGitHub) -> None:
    old, _ = _release_commit(repo, gh, migration=False)
    repo.commit("releases/v1.0.0", "late.txt", "late\n")
    runner = RecordingRunner(old)
    with pytest.raises(ReleaseError, match="已过期"):
        deploy.deploy(repo.git, gh, deploy.TARGETS["staging"], "v1.0.0-rc.1", run=runner)
    assert runner.calls == []


def test_production_deploy_requires_owner_approval(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy, "http_ok", lambda url: True)
    old, sha = _release_commit(repo, gh, migration=False)
    gh.statuses[sha] = "success"
    seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    runner = RecordingRunner(old)
    target = deploy.TARGETS["production"]
    with pytest.raises(ReleaseError, match="审批"):
        deploy.deploy(repo.git, gh, target, "v1.0.0", run=runner)
    assert runner.calls == []  # nothing ran on the server

    gh.jobs.add(
        (
            deploy.PRODUCTION_WORKFLOW,
            deploy.production_title("v1.0.0"),
            deploy.APPROVE_JOB,
            "production",
        )
    )
    runner.code = 7  # failed and rolled back on the host
    result = deploy.deploy(repo.git, gh, target, "v1.0.0", run=runner, log=lambda _: None)
    assert result.code == 7
    assert "已自动回滚" in result.lines[-1]
    args, _ = runner.calls[-1]
    assert args[:2] == ["ssh", "greencloud-tyo-01"]
    assert "--migrations unchanged" in args[2]
    assert "--render-template" not in args[2]
    # Retrying the same tag is allowed (the tag never moves).
    runner.code = 0
    assert deploy.deploy(repo.git, gh, target, "v1.0.0", run=runner, log=lambda _: None).code == 0


def test_migration_detection_is_conservative(repo: Repo) -> None:
    head = repo.sha("refs/heads/master")
    assert deploy.migrations_changed(repo.git, "latest", head)
    assert deploy.migrations_changed(repo.git, "0" * 40, head)  # unknown commit
    assert not deploy.migrations_changed(repo.git, head, head)


def test_environment_lock_is_exclusive(tmp_path: Path) -> None:
    with (
        deploy.environment_lock("staging", tmp_path),
        pytest.raises(ReleaseError, match="占用"),
        deploy.environment_lock("staging", tmp_path),
    ):
        pass
    with deploy.environment_lock("staging", tmp_path):
        pass


def test_rollback_across_migration_is_refused(repo: Repo, gh: FakeGitHub) -> None:
    _, sha = _release_commit(repo, gh, migration=False)
    gh.statuses[sha] = "success"
    seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    gh.jobs.add(
        (
            deploy.PRODUCTION_WORKFLOW,
            deploy.production_title("v1.0.0"),
            deploy.APPROVE_JOB,
            "production",
        )
    )
    run_cascade(repo.git, gh, trigger="forward v1.0.0")
    newer = repo.commit("master", f"{deploy.MIGRATIONS_DIR}/0002_y.py", "y\n")  # running version
    runner = RecordingRunner(newer)
    with pytest.raises(ReleaseError, match=r"§5\.2"):
        deploy.deploy(repo.git, gh, deploy.TARGETS["production"], "v1.0.0", run=runner)
    assert len(runner.calls) == 1  # only read the current version
