"""Guard: tracked files must not contain values shaped like real credentials.

Test and documentation samples follow the rule in ``AGENTS.md`` ("测试与文档中的
凭据样例"): they must be obviously fake and must *not* match the format of the
real credential. This test scans every tracked text file for the real Telegram
bot token shape (numeric bot id, ``:``, then ``A`` followed by 33-34 token
characters; real secrets are 35 characters starting with ``AA``), which is the
shape GitHub secret scanning reports.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Real Telegram bot token shape, deliberately a bit wider than the 35-char
#: ``AA...`` secret so near-real samples (e.g. the Bot API docs example) fail too.
TELEGRAM_BOT_TOKEN = re.compile(r"(?<![0-9])\d{5,}:A[A-Za-z0-9_-]{33,34}(?![A-Za-z0-9_-])")

#: The placeholder the repository uses instead.
PLACEHOLDER = "123456:fake-bot-token-for-tests"


def _tracked_files() -> list[Path]:
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        [git, "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    return [REPO_ROOT / name for name in out.decode().split("\0") if name]


def _matches(text: str) -> bool:
    return TELEGRAM_BOT_TOKEN.search(text) is not None


def test_pattern_flags_real_shape_but_not_placeholder() -> None:
    # Built at runtime from obviously fake parts so this file itself stays clean.
    real_shape = "1" * 10 + ":AA" + "x" * 33
    assert _matches(f"token={real_shape}")
    assert _matches("1" * 6 + ":A" + "x" * 33)
    assert not _matches(PLACEHOLDER)
    assert not _matches("123:bot-token-value")
    assert not _matches("1" * 10 + ":AA" + "x" * 40)


def test_no_tracked_file_contains_real_telegram_token_shape() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _matches(line):
                # Report the location only, never the value.
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "以下位置出现了真实 Telegram bot token 格式的值，请改用明显不可用的占位值"
        f"（如 {PLACEHOLDER!r}，见 AGENTS.md）：{offenders}"
    )
