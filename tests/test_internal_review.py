"""Tests for the private pre-review push gate (scripts/internal_review.py).

All shas, agent ids and comments here are fictional fixtures.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.internal_review import (
    GitError,
    check_push,
    check_sync_remote,
    git,
    install_guard,
    latest_verdict,
    ref_guard,
    repo_slug,
)

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
        comment(REVIEWER, "2026-01-01T00:00:02Z", f"说明\n{approval(a, '撤销')}\n{approval(b)}"),
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
    assert install_guard() == 0
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


@pytest.mark.parametrize(
    "content",
    [
        # Quoted, indented-in-quote and fenced examples are not verdicts.
        "> {line}",
        "引用：\n>{line}",
        "示例：\n```\n{line}\n```",
        "示例：\n~~~text\n{line}\n~~~",
        "`{line}`",
        "  {line}",
        "结论 {line}",
    ],
)
def test_quotes_and_code_are_not_verdicts(content: str) -> None:
    line = approval("e" * 40)
    comments = [comment(REVIEWER, "2026-01-01T00:00:01Z", content.format(line=line))]
    assert latest_verdict(comments, {REVIEWER}) is None


def test_quoting_old_approval_does_not_undo_revoke() -> None:
    a = "a" * 40
    comments = [
        comment(REVIEWER, "2026-01-01T00:00:01Z", approval(a)),
        comment(REVIEWER, "2026-01-01T00:00:02Z", approval(a, "撤销")),
        comment(
            REVIEWER,
            "2026-01-01T00:00:03Z",
            f"回复上一条：\n> {approval(a)}\n\n```\n{approval(a)}\n```",
        ),
    ]
    verdict = latest_verdict(comments, {REVIEWER})
    assert verdict is not None
    assert not verdict.approved


def guard_lines(*updates: tuple[str, str]) -> list[str]:
    return [f"refs/heads/{ref} {sha} refs/heads/{ref} {'0' * 40}" for ref, sha in updates]


def test_ref_guard_rejects_second_ref_with_content(repo: Path) -> None:
    old = run_git(repo, "rev-parse", "HEAD~1")  # already public
    new = run_git(repo, "rev-parse", "HEAD")
    # The unchecked ref is the second one: pre-commit would only look at the first.
    allowed, message = ref_guard(
        PUBLIC_URL, guard_lines(("releases/v9.9.0", old), ("akkia-7-fixture", new))
    )
    assert not allowed
    assert "akkia-7-fixture" in message


def test_ref_guard_allows_single_ref_and_content_free_pushes(repo: Path) -> None:
    old = run_git(repo, "rev-parse", "HEAD~1")
    new = run_git(repo, "rev-parse", "HEAD")
    deletion = f"refs/heads/gone {'0' * 40} refs/heads/gone {old}"
    assert ref_guard(PUBLIC_URL, guard_lines(("akkia-7-fixture", new)))[0]
    assert ref_guard(PUBLIC_URL, [*guard_lines(("akkia-7-fixture", new)), deletion])[0]
    assert ref_guard(PUBLIC_URL, guard_lines(("a", old), ("b", old)))[0]
    private = "https://github.com/AkkiaLab/AkkiaBot-internal.git"
    assert ref_guard(private, guard_lines(("a", new), ("b", new)))[0]


def test_installed_guard_reads_full_stdin(repo: Path) -> None:
    """The installed pre-push.legacy wrapper enforces the guard on real hook input."""
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "internal_review.py").write_text(
        (Path(__file__).resolve().parents[1] / "scripts" / "internal_review.py").read_text("utf-8"),
        "utf-8",
    )
    hook = (
        Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        / "pre-push.legacy"
    )
    old = run_git(repo, "rev-parse", "HEAD~1")
    new = run_git(repo, "rev-parse", "HEAD")
    stdin = "\n".join(guard_lines(("releases/v9.9.0", old), ("akkia-7-fixture", new))) + "\n"
    result = subprocess.run(
        [str(hook), "origin", PUBLIC_URL],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "ref-guard" in result.stderr
    single = "\n".join(guard_lines(("akkia-7-fixture", new))) + "\n"
    result = subprocess.run(
        [str(hook), "origin", PUBLIC_URL],
        cwd=repo,
        input=single,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_check_push_requires_guard(repo: Path, fake_cli: tuple[Path, Path]) -> None:
    hook = (
        Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        / "pre-push.legacy"
    )
    hook.unlink()
    cli, data = fake_cli
    head = run_git(repo, "rev-parse", "HEAD")
    data.write_text(
        json.dumps([comment(REVIEWER, "2026-01-01T00:00:01Z", approval(head))]), "utf-8"
    )
    allowed, message = check_push(push_env(repo, cli))
    assert not allowed
    assert "just setup" in message


def test_install_guard_keeps_foreign_hook(repo: Path) -> None:
    hook = (
        Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
        / "pre-push.legacy"
    )
    hook.write_text("#!/bin/sh\nexit 0\n", "utf-8")
    assert install_guard() == 1
    assert hook.read_text("utf-8") == "#!/bin/sh\nexit 0\n"


def test_sync_remote_must_push_only_to_private(repo: Path) -> None:
    assert check_sync_remote("internal") is None
    # fetch URL is private but an extra push URL points elsewhere
    run_git(
        repo,
        "remote",
        "set-url",
        "--add",
        "--push",
        "internal",
        "https://github.com/AkkiaLab/AkkiaBot-internal.git",
    )
    run_git(repo, "remote", "set-url", "--add", "--push", "internal", PUBLIC_URL)
    assert check_sync_remote("internal") is not None
    run_git(repo, "remote", "set-url", "--delete", "--push", "internal", "AkkiaBot.git$")
    assert check_sync_remote("internal") is None
    run_git(repo, "config", "remote.internal.mirror", "true")
    assert check_sync_remote("internal") is not None
    run_git(repo, "config", "--unset", "remote.internal.mirror")
    # pushInsteadOf only rewrites when there is no explicit pushurl.
    run_git(repo, "config", "--unset-all", "remote.internal.pushurl")
    assert check_sync_remote("internal") is None
    run_git(
        repo,
        "config",
        f"url.{PUBLIC_URL}.pushInsteadOf",
        "https://github.com/AkkiaLab/AkkiaBot-internal.git",
    )
    assert check_sync_remote("internal") is not None
    assert check_sync_remote("origin") is not None


def test_git_error_hides_args_and_stderr(repo: Path) -> None:
    mock_url = (
        "https://user:mock-not-a-token@example.invalid/x.git"  # mock: fictional credential URL
    )
    with pytest.raises(GitError) as exc:
        git("ls-remote", mock_url)
    assert "mock-not-a-token" not in str(exc.value)
    assert "example.invalid" not in str(exc.value)
