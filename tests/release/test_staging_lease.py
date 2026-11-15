"""Staging lease and the staging release sequence (Mini App → backend → acceptance)."""

import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from scripts.release import deploy, lease
from scripts.release.branches import create_release
from scripts.release.git import Git
from scripts.release.rc import create_rc
from scripts.release.seal import seal
from scripts.release.versions import ReleaseError, Version
from tests.release.conftest import FakeGitHub, Repo

V = Version.parse
NOW = datetime(2026, 11, 19, 10, 30, tzinfo=UTC)


def _two_rcs(repo: Repo, gh: FakeGitHub) -> tuple[str, str]:
    create_release(repo.git, V("v1.0.0"))
    repo.commit("releases/v1.0.0", "deploy/upgrade.sh", "#!/bin/sh\n")
    a, _ = create_rc(repo.git, gh, V("v1.0.0"))
    repo.commit("master", "m.txt", "m\n")
    create_release(repo.git, V("v1.1.0"))
    repo.commit("releases/v1.1.0", "deploy/upgrade.sh", "#!/bin/sh\necho b\n")
    b, _ = create_rc(repo.git, gh, V("v1.1.0"))
    return a.sha, b.sha


def test_lease_is_exclusive_and_checked_per_sha(repo: Repo, gh: FakeGitHub) -> None:
    a, b = _two_rcs(repo, gh)
    other = Git(repo.dev)  # a second machine / an Actions runner
    held = lease.acquire(repo.git, a, "v1.0.0-rc.1", now=NOW)
    assert lease.check(other, a, now=NOW).label == "v1.0.0-rc.1"
    with pytest.raises(ReleaseError, match="拒绝覆盖"):
        lease.check(other, b, now=NOW)  # manual / second RC frontend upload refused
    with pytest.raises(ReleaseError, match="占用"):
        lease.acquire(other, b, "v1.1.0-rc.1", now=NOW)  # interleaving RC refused
    assert lease.release(repo.git, held)
    with pytest.raises(ReleaseError, match="没有租约"):
        lease.check(other, a, now=NOW)
    assert lease.acquire(other, b, "v1.1.0-rc.1", now=NOW).sha == b


def test_expired_lease_is_taken_over_atomically(repo: Repo, gh: FakeGitHub) -> None:
    a, b = _two_rcs(repo, gh)
    stale = lease.acquire(repo.git, a, "v1.0.0-rc.1", now=NOW, ttl=timedelta(hours=1))
    later = NOW + timedelta(hours=2)
    with pytest.raises(ReleaseError, match="过期"):
        lease.check(repo.git, a, now=later)
    taken = lease.acquire(Git(repo.dev), b, "v1.1.0-rc.1", now=later)
    assert not lease.release(repo.git, stale)  # the old holder can no longer release it
    assert lease.check(repo.git, b, now=later).commit == taken.commit


def test_release_staging_holds_the_lease_until_acceptance_ends(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_rcs(repo, gh)
    monkeypatch.setattr(deploy, "template_environment", lambda: {})
    monkeypatch.setattr(deploy, "http_ok", lambda url: True)
    monkeypatch.setattr(deploy, "fetch_json", lambda url: {"sha": a})
    observed: list[str] = []

    def frontend(inputs: dict[str, str]) -> None:
        # What webapp-deploy.yml checks right before its upload.
        observed.append(lease.check(Git(repo.dev), inputs["sha"]).label)
        with pytest.raises(ReleaseError, match="拒绝覆盖"):
            lease.check(Git(repo.dev), b)  # a second RC run at the same time

    gh.on_run = frontend

    def accept(argv: list[str]) -> int:
        observed.append("accept")
        with pytest.raises(ReleaseError, match="占用"):
            lease.acquire(Git(repo.dev), b, "v1.1.0-rc.1")  # still held during acceptance
        return 0

    def host(
        args: list[str], stdin: str | None, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        observed.append("backend" if stdin else "read")
        return subprocess.CompletedProcess(args, 0, "0" * 40 if stdin is None else "", "")

    result = deploy.release_staging(
        repo.git,
        gh,
        deploy.TARGETS["staging"],
        "v1.0.0-rc.1",
        scenarios=None,
        accept=True,
        run=host,
        accept_run=accept,
        log=lambda _: None,
    )
    assert result.code == 0, result.lines
    assert observed == ["v1.0.0-rc.1", "read", "backend", "accept"]  # strict order
    assert gh.runs[0][1]["sha"] == a
    assert lease.read_lease(repo.git) is None  # released afterwards


@pytest.mark.parametrize(
    ("frontend", "health", "version", "code"),
    [
        ("failure", True, "a", 1),  # Mini App upload failed: backend untouched
        ("success", False, "a", deploy.PUBLIC_HEALTH_FAILED),
        ("success", True, None, deploy.WEBAPP_VERSION_MISMATCH),  # unreadable
        ("success", True, "other", deploy.WEBAPP_VERSION_MISMATCH),
    ],
)
def test_release_staging_stops_on_any_failed_check(
    repo: Repo,
    gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    frontend: str,
    health: bool,
    version: str | None,
    code: int,
) -> None:
    a, _ = _two_rcs(repo, gh)
    seen = {"a": a, "other": "1" * 40, None: None}[version]
    monkeypatch.setattr(deploy, "template_environment", lambda: {})
    monkeypatch.setattr(deploy, "http_ok", lambda url: health)
    monkeypatch.setattr(deploy, "fetch_json", lambda url: {"sha": seen} if seen else None)
    gh.run_conclusion = frontend
    calls: list[str | None] = []
    accepted: list[list[str]] = []

    def host(
        args: list[str], stdin: str | None, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(stdin)
        return subprocess.CompletedProcess(args, 0, "0" * 40 if stdin is None else "", "")

    result = deploy.release_staging(
        repo.git,
        gh,
        deploy.TARGETS["staging"],
        "v1.0.0-rc.1",
        scenarios=None,
        accept=True,
        run=host,
        accept_run=lambda argv: accepted.append(argv) or 0,
        log=lambda _: None,
    )
    assert result.code == code
    assert accepted == []  # never reaches the acceptance
    if frontend == "failure":
        assert calls == []  # the backend was not touched
    assert lease.read_lease(repo.git) is None


def test_production_public_health_failure_stops_before_frontend(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_release(repo.git, V("v1.0.0"))
    repo.commit("releases/v1.0.0", "deploy/upgrade.sh", "#!/bin/sh\n")
    candidate, _ = create_rc(repo.git, gh, V("v1.0.0"))
    gh.statuses[candidate.sha] = "success"
    seal(repo.git, gh, V("v1.0.0"), log=lambda _: None)
    gh.jobs.add(
        (
            deploy.PRODUCTION_WORKFLOW,
            deploy.production_title("v1.0.0"),
            deploy.APPROVE_JOB,
            "production",
        )
    )
    monkeypatch.setattr(deploy, "http_ok", lambda url: False)

    def host(
        args: list[str], stdin: str | None, env: dict[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "0" * 40 if stdin is None else "", "")

    result = deploy.deploy(
        repo.git, gh, deploy.TARGETS["production"], "v1.0.0", run=host, log=lambda _: None
    )
    assert result.code == deploy.PUBLIC_HEALTH_FAILED
    assert "公网 /readyz" in result.lines[-1]


def test_cli_production_does_not_promote_frontend_after_failed_check(
    repo: Repo, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.release import cli

    failed = deploy.DeployResult("a" * 40, "b" * 40, deploy.PUBLIC_HEALTH_FAILED, ["x"])
    monkeypatch.setattr(deploy, "deploy", lambda *a, **k: failed)
    monkeypatch.setattr(
        deploy, "environment_lock", lambda name: __import__("contextlib").nullcontext()
    )
    args = cli.build_parser().parse_args(["deploy", "production", "v1.0.0"])
    assert cli.cmd_deploy(repo.git, gh, args) == deploy.PUBLIC_HEALTH_FAILED
    assert gh.dispatched == []  # webapp-promote.yml not triggered
