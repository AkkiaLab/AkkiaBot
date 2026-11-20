"""Tests for the private pre-review push gate (scripts/internal_review.py).

All shas, agent ids and comments here are fictional fixtures.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.internal_review import check_push, latest_verdict, repo_slug

PUBLIC_URL = "https://github.com/AkkiaLab/AkkiaBot.git"
REVIEWER = "reviewer-agent-fixture"
OTHER = "implementer-agent-fixture"


def approval(sha: str, word: str = "通过") -> str:
    return f"预审{word}：AkkiaLab/AkkiaBot-internal@{sha}"


def comment(author: str, created: str, content: str) -> dict[str, object]:
    return {
        "id": f"c-{created}",
        "author_type": "agent",
        "author_id": author,
        "created_at": created,
        "content": content,
    }


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/AkkiaLab/AkkiaBot.git", "AkkiaLab/AkkiaBot"),
        ("https://github.com/AkkiaLab/AkkiaBot", "AkkiaLab/AkkiaBot"),
        ("git@github.com:AkkiaLab/AkkiaBot.git", "AkkiaLab/AkkiaBot"),
        ("https://github.com/AkkiaLab/AkkiaBot-internal.git", "AkkiaLab/AkkiaBot-internal"),
        ("/srv/git/AkkiaBot.git", None),
    ],
)
def test_repo_slug(url: str, slug: str | None) -> None:
    assert repo_slug(url) == slug


def test_latest_verdict_ignores_non_reviewers_and_takes_newest() -> None:
    a, b = "a" * 40, "b" * 40
    comments = [
        comment(REVIEWER, "2026-01-01T00:00:01Z", approval(a)),
        comment(OTHER, "2026-01-01T00:00:03Z", approval(b)),
        comment(REVIEWER, "2026-01-01T00:00:02Z", f"说明\n`{approval(a, '撤销')}`\n{approval(b)}"),
    ]
    verdict = latest_verdict(comments, {REVIEWER})
    assert verdict is not None
    assert (verdict.approved, verdict.sha) == (True, b)


def test_latest_verdict_requires_full_sha_and_line() -> None:
    comments = [
        comment(
            REVIEWER, "2026-01-01T00:00:01Z", "格式：预审通过：AkkiaLab/AkkiaBot-internal@<SHA>"
        ),
        comment(REVIEWER, "2026-01-01T00:00:02Z", approval("c" * 12)),
        comment(REVIEWER, "2026-01-01T00:00:03Z", "预审通过：AkkiaLab/AkkiaBot@" + "d" * 40),
    ]
    assert latest_verdict(comments, {REVIEWER}) is None


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose ``origin`` points at the public URL; base is on origin, head is new."""
    path = tmp_path / "repo"
    path.mkdir()
    run_git(path, "init", "-q", "-b", "main")
    run_git(path, "config", "user.email", "fixture@example.invalid")
    run_git(path, "config", "user.name", "fixture")
    run_git(path, "remote", "add", "origin", PUBLIC_URL)
    run_git(path, "remote", "add", "internal", "https://github.com/AkkiaLab/AkkiaBot-internal.git")
    run_git(path, "commit", "-q", "--allow-empty", "-m", "base")
    run_git(path, "update-ref", "refs/remotes/origin/releases/v9.9.0", "HEAD")
    run_git(path, "switch", "-q", "-c", "akkia-7-fixture")
    run_git(path, "commit", "-q", "--allow-empty", "-m", "new work")
    # Already on the private repo: must not count as "already public".
    run_git(path, "update-ref", "refs/remotes/internal/akkia-7-fixture", "HEAD")
    run_git(path, "config", "--add", "akkia.preapproval.reviewer", REVIEWER)
    monkeypatch.chdir(path)
    return path


@pytest.fixture
def fake_cli(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in multica CLI that prints ``comments.json`` and logs its argv."""
    data = tmp_path / "comments.json"
    data.write_text("[]", "utf-8")
    log = tmp_path / "argv.json"
    script = tmp_path / "multica"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"sys.stdout.write(open({str(data)!r}).read())\n",
        "utf-8",
    )
    script.chmod(0o755)
    return script, data


def push_env(repo: Path, cli: Path | None, **extra: str) -> dict[str, str]:
    env = {
        "PRE_COMMIT_REMOTE_NAME": "origin",
        "PRE_COMMIT_REMOTE_URL": PUBLIC_URL,
        "PRE_COMMIT_TO_REF": run_git(repo, "rev-parse", "HEAD"),
        "PRE_COMMIT_LOCAL_BRANCH": "refs/heads/akkia-7-fixture",
        "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/akkia-7-fixture",
        "PATH": os.environ.get("PATH", ""),
    }
    if cli is not None:
        env["MULTICA_CLI_PATH"] = str(cli)
    env.update(extra)
    return env


def test_private_push_is_not_gated(repo: Path) -> None:
    env = push_env(
        repo, None, PRE_COMMIT_REMOTE_URL="https://github.com/AkkiaLab/AkkiaBot-internal.git"
    )
    assert check_push(env)[0]


def test_push_without_new_commits_passes(repo: Path) -> None:
    env = push_env(repo, None, PRE_COMMIT_TO_REF=run_git(repo, "rev-parse", "HEAD~1"))
    allowed, message = check_push(env)
    assert allowed, message


def test_branch_deletion_passes(repo: Path) -> None:
    assert check_push(push_env(repo, None, PRE_COMMIT_TO_REF="0" * 40))[0]


def test_no_approval_rejected(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    cli, _ = fake_cli
    allowed, message = check_push(push_env(repo, cli))
    assert not allowed
    assert "AKKIA-7" in message
    argv = json.loads((cli.parent / "argv.json").read_text("utf-8"))
    assert argv[:4] == ["issue", "comment", "list", "AKKIA-7"]


def test_sha_mismatch_rejected(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    cli, data = fake_cli
    stale = run_git(repo, "rev-parse", "HEAD~1")
    data.write_text(
        json.dumps([comment(REVIEWER, "2026-01-01T00:00:01Z", approval(stale))]), "utf-8"
    )
    allowed, message = check_push(push_env(repo, cli))
    assert not allowed
    assert stale in message


def test_matching_approval_passes(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    cli, data = fake_cli
    head = run_git(repo, "rev-parse", "HEAD")
    data.write_text(
        json.dumps([comment(REVIEWER, "2026-01-01T00:00:01Z", approval(head))]), "utf-8"
    )
    allowed, message = check_push(push_env(repo, cli))
    assert allowed, message


def test_revoked_approval_rejected(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    cli, data = fake_cli
    head = run_git(repo, "rev-parse", "HEAD")
    data.write_text(
        json.dumps(
            [
                comment(REVIEWER, "2026-01-01T00:00:01Z", approval(head)),
                comment(REVIEWER, "2026-01-01T00:00:02Z", approval(head, "撤销")),
            ]
        ),
        "utf-8",
    )
    assert not check_push(push_env(repo, cli))[0]


def test_approval_by_other_agent_rejected(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    cli, data = fake_cli
    head = run_git(repo, "rev-parse", "HEAD")
    data.write_text(json.dumps([comment(OTHER, "2026-01-01T00:00:01Z", approval(head))]), "utf-8")
    assert not check_push(push_env(repo, cli))[0]


def test_branch_without_issue_number_rejected(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    cli, _ = fake_cli
    env = push_env(
        repo,
        cli,
        PRE_COMMIT_LOCAL_BRANCH="refs/heads/topic",
        PRE_COMMIT_REMOTE_BRANCH="refs/heads/topic",
    )
    allowed, message = check_push(env)
    assert not allowed
    assert "akkia-<编号>" in message


def test_missing_reviewer_config_rejected(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    run_git(repo, "config", "--unset-all", "akkia.preapproval.reviewer")
    cli, _ = fake_cli
    assert not check_push(push_env(repo, cli))[0]


def test_missing_cli_rejected(repo: Path) -> None:
    env = push_env(repo, None, PATH="/nonexistent")
    allowed, message = check_push(env)
    assert not allowed
    assert "multica" in message


def test_secret_scan_excludes_only_origin() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "secret_scan.sh").read_text("utf-8")
    assert "--not --remotes=origin" in script
    assert "--not --remotes)" not in script
    assert '--not --remotes"' not in script


def test_pre_push_hooks_registered() -> None:
    config = (Path(__file__).resolve().parents[1] / ".pre-commit-config.yaml").read_text("utf-8")
    for hook in (
        "id: test-secret-samples",
        "id: preapproval-check",
        "scripts/internal_review.py check-push",
    ):
        assert hook in config
