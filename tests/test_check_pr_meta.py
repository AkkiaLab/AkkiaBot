import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_pr_meta import check

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_meta.py"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pr-title.yml"

# MOCK: synthetic marker standing in for sensitive text a PR body might carry.
# It must never reach the job log (stdout/stderr) in any path.
MOCK_BODY_MARKER = "MOCK-BODY-MARKER-not-a-secret-7f3a"
MOCK_TITLE_MARKER = "MOCK-TITLE-MARKER-9c1e"


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("BOT-6: 搭建 monorepo 骨架与 CI", "Closes BOT-6\n\n正文"),
        ("BOT-12: 修复", "说明\r\nCloses BOT-12\r\n"),
        ("BOT-7: x", "Closes BOT-7 and more"),
    ],
)
def test_valid(title: str, body: str) -> None:
    assert check(title, body) == []


@pytest.mark.parametrize(
    ("title", "body", "fragment"),
    [
        ("搭建 monorepo 骨架", "Closes BOT-6", "PR 标题"),
        ("bot-6: lower case", "Closes BOT-6", "PR 标题"),
        ("BOT-6 missing colon", "Closes BOT-6", "PR 标题"),
        ("BOT-6:", "Closes BOT-6", "PR 标题"),
        ("BOT-6: ok", "", "Closes BOT-"),
        ("BOT-6: ok", "关联 BOT-6", "Closes BOT-"),
        ("BOT-6: ok", "Closes BOT-7", "不一致"),
        ("BOT-6: ok", "Closes BOT-60", "不一致"),
    ],
)
def test_invalid(title: str, body: str, fragment: str) -> None:
    errors = check(title, body)
    assert errors
    assert any(fragment in e for e in errors)


def _run(
    tmp_path: Path, event: object | None, *, raw: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin"}
    if event is not None or raw is not None:
        path = tmp_path / "event.json"
        path.write_text(raw if raw is not None else json.dumps(event), encoding="utf-8")
        env["GITHUB_EVENT_PATH"] = str(path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _pr(title: object, body: object) -> dict[str, object]:
    return {"action": "edited", "pull_request": {"title": title, "body": body}}


def _assert_no_leak(result: subprocess.CompletedProcess[str]) -> None:
    log = result.stdout + result.stderr
    assert MOCK_BODY_MARKER not in log
    assert MOCK_TITLE_MARKER not in log


def test_cli_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, _pr("BOT-6: 搭建骨架", f"Closes BOT-6\n\n{MOCK_BODY_MARKER}"))
    assert result.returncode == 0
    _assert_no_leak(result)


@pytest.mark.parametrize(
    ("title", "body"),
    [
        (f"{MOCK_TITLE_MARKER} 搭建骨架", f"Closes BOT-6\n{MOCK_BODY_MARKER}"),
        ("BOT-6: ok", f"{MOCK_BODY_MARKER}"),
        ("BOT-6: ok", f"Closes BOT-7\n{MOCK_BODY_MARKER}"),
        # Shell metacharacters stay plain data: nothing is expanded or executed.
        (
            f"BOT-6: $(echo {MOCK_TITLE_MARKER})",
            f"Closes BOT-6\n`echo {MOCK_BODY_MARKER}` ${{{{ x }}}}",
        ),
    ],
)
def test_cli_never_echoes_title_or_body(tmp_path: Path, title: str, body: str) -> None:
    result = _run(tmp_path, _pr(title, body))
    _assert_no_leak(result)


@pytest.mark.parametrize(
    ("title", "body"),
    [
        (f"{MOCK_TITLE_MARKER} 搭建骨架", f"Closes BOT-6\n{MOCK_BODY_MARKER}"),
        ("BOT-6: ok", MOCK_BODY_MARKER),
        ("BOT-6: ok", f"Closes BOT-7\n{MOCK_BODY_MARKER}"),
    ],
)
def test_cli_fails_with_rule_only_message(tmp_path: Path, title: str, body: str) -> None:
    result = _run(tmp_path, _pr(title, body))
    assert result.returncode == 1
    assert "::error::" in result.stdout
    _assert_no_leak(result)


def test_cli_null_body_is_treated_as_empty(tmp_path: Path) -> None:
    result = _run(tmp_path, _pr("BOT-6: ok", None))
    assert result.returncode == 1
    assert "Closes BOT-" in result.stdout


def test_cli_ignores_legacy_env(tmp_path: Path) -> None:
    # The old PR_TITLE/PR_BODY env path is gone; without an event file it fails closed.
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "PR_TITLE": "BOT-6: ok",
            "PR_BODY": f"Closes BOT-6 {MOCK_BODY_MARKER}",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "GITHUB_EVENT_PATH" in result.stdout
    _assert_no_leak(result)


@pytest.mark.parametrize(
    ("event", "raw"),
    [
        (None, None),  # GITHUB_EVENT_PATH unset
        (None, f"{{not json {MOCK_BODY_MARKER}"),
        ({"push": {"body": MOCK_BODY_MARKER}}, None),
        ([MOCK_BODY_MARKER], None),
    ],
)
def test_cli_bad_event_fails_without_dumping_payload(
    tmp_path: Path, event: object, raw: str | None
) -> None:
    result = _run(tmp_path, event, raw=raw)
    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "Traceback" not in result.stderr
    _assert_no_leak(result)


def test_workflow_does_not_expand_pr_text() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for field in ("pull_request.body", "pull_request.title"):
        assert field not in text, f"pr-title.yml must not expand {field} into env/run/with"
