import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_pr_meta import check

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_meta.py"


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


def _run(title: str, body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={"PR_TITLE": title, "PR_BODY": body},
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_fails_without_bot_number() -> None:
    result = _run("搭建骨架", "Closes BOT-6")
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_cli_passes() -> None:
    assert _run("BOT-6: 搭建骨架", "Closes BOT-6").returncode == 0
