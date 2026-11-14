"""Guards for credential samples in tracked files (see ``AGENTS.md``, "测试与文档中的凭据样例").

1. No tracked text file may contain the real Telegram bot token shape (numeric
   bot id, ``:``, then ``A`` followed by 33-34 token characters; real secrets are
   35 characters starting with ``AA``), which is what GitHub secret scanning
   reports.
2. Every credential-shaped literal in tracked Python files must be declared where
   it lives as a mock: assigned to a ``MOCK_``/``mock_`` name, with a ``# mock``
   comment on (or right above) the assignment. Inline uses and prefix-for-concat
   literals such as ``"123456:" + ...`` are rejected.

Failures report locations only, never values.
"""

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Real Telegram bot token shape, deliberately a bit wider than the 35-char
#: ``AA...`` secret so near-real samples (e.g. the Bot API docs example) fail too.
TELEGRAM_BOT_TOKEN = re.compile(r"(?<![0-9])\d{5,}:A[A-Za-z0-9_-]{33,34}(?![A-Za-z0-9_-])")

#: Literals that stand in for a credential: Telegram bot tokens (``<id>:<secret>``,
#: but not IPv6 such as ``2001:db8::1``), GitHub / 1Password token prefixes, Sentry
#: DSNs, Telegram api_hash, webhook secrets and Telethon session strings.
CREDENTIAL_LIKE = re.compile(
    r"(?<![\w:])\d+:[A-Za-z][A-Za-z0-9_-]*(?![A-Za-z0-9_:-])"
    r"|\b(?:ghs|ghp|gho|ghu|github_pat|ops)_[A-Za-z0-9_-]+"
    r"|https?://[^/@\s]+@[^/\s]*sentry"
    r"|api-hash|webhook-secret|SessionString"
)

#: A bare ``"<digits>:"`` literal only exists to be concatenated into a token.
TOKEN_PREFIX = re.compile(r"^\d+:$")

MOCK_NAME = re.compile(r"^_?mock_", re.IGNORECASE)
MOCK_COMMENT = re.compile(r"#.*\bmock\b", re.IGNORECASE)

# mock: obviously fake, wrong-format placeholder the repository uses instead.
MOCK_BOT_TOKEN = "123456:fake-bot-token-for-tests"
# mock: deliberately malformed (id too short) sample of the same family.
MOCK_SHORT_BOT_TOKEN = "123:bot-token-value"


def _tracked_files(*patterns: str) -> list[Path]:
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        [git, "ls-files", "-z", "--", *patterns], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    return [REPO_ROOT / name for name in out.decode().split("\0") if name]


def _matches(text: str) -> bool:
    return TELEGRAM_BOT_TOKEN.search(text) is not None


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]
    else:
        return []
    return [
        t.id if isinstance(t, ast.Name) else t.attr
        for t in targets
        if isinstance(t, ast.Name | ast.Attribute)
    ]


def _is_regex_source(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    call = parents.get(node)
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "compile"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "re"
    )


def unmarked_credential_literals(source: str) -> list[int]:
    """Line numbers of credential-shaped literals not declared as a mock in place."""
    tree = ast.parse(source)
    lines = source.splitlines()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    bad: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if TOKEN_PREFIX.match(node.value):
            bad.append(node.lineno)
            continue
        if not CREDENTIAL_LIKE.search(node.value) or _is_regex_source(node, parents):
            continue
        holder: ast.AST | None = parents.get(node)
        while holder is not None and not isinstance(holder, ast.stmt):
            holder = parents.get(holder)
        names = _target_names(holder) if holder is not None else []
        ok = False
        if holder is not None and names and all(MOCK_NAME.match(n) for n in names):
            first = max(holder.lineno - 2, 0)
            last = holder.end_lineno or holder.lineno
            ok = any(MOCK_COMMENT.search(line) for line in lines[first:last])
        if not ok:
            bad.append(node.lineno)
    return sorted(set(bad))


def test_pattern_flags_real_shape_but_not_placeholder() -> None:
    # Built at runtime from obviously fake parts so this file itself stays clean.
    real_shape = "1" * 10 + ":AA" + "x" * 33
    assert _matches(f"token={real_shape}")
    assert _matches("1" * 6 + ":A" + "x" * 33)
    assert not _matches(MOCK_BOT_TOKEN)
    assert not _matches(MOCK_SHORT_BOT_TOKEN)
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
        f"（如 {MOCK_BOT_TOKEN!r}，见 AGENTS.md）：{offenders}"
    )


# The cases below are parsed as source text, never executed; they are not
# credential samples themselves, so the literals are assembled from pieces that
# do not match on their own.
_ID, _TOK = "123456", "fake-bot-token-for-tests"


@pytest.mark.parametrize(
    "source",
    [
        f'MOCK_BOT_TOKEN = "{_ID}:{_TOK}"  # mock: fake\n',
        f'# mock: fake, not a credential\nMOCK_BOT_TOKEN = "{_ID}:{_TOK}"\n',
        f'def f():\n    mock_token = "{_ID}:{_TOK}"  # mock\n',
        'ADDR = "2001:db8::1"\nTIME = "12:30"\nURL = "postgresql://u:p@db:5432/x"\n',
    ],
    ids=["inline-comment", "comment-above", "local-name", "not-credential-like"],
)
def test_mock_declaration_accepted(source: str) -> None:
    assert unmarked_credential_literals(source) == []


@pytest.mark.parametrize(
    "source",
    [
        f'BOT_TOKEN = "{_ID}:{_TOK}"  # mock: fake\n',
        f'MOCK_BOT_TOKEN = "{_ID}:{_TOK}"\n',
        f'call(token="{_ID}:{_TOK}")  # mock\n',
        f'MOCK_BOT_TOKEN = "{_ID}:" + "A" * 35  # mock\n',
        'MOCK_DSN = "https://key@o0.ingest.' + 'sentry.io/1"\n',
    ],
    ids=["no-mock-name", "no-mock-comment", "inline-use", "concat-prefix", "dsn-no-comment"],
)
def test_unmarked_credential_literal_rejected(source: str) -> None:
    assert unmarked_credential_literals(source) != []


def test_credential_like_literals_are_declared_as_mocks() -> None:
    offenders: list[str] = []
    for path in _tracked_files("*.py"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        offenders += [f"{rel}:{n}" for n in unmarked_credential_literals(path.read_text("utf-8"))]
    assert not offenders, (
        "以下凭据形字面量未就地声明为 mock：请赋给 MOCK_/mock_ 开头的名称，"
        f"并在同一行或上一行加 `# mock: ...` 注释说明人工构造、不可用（见 AGENTS.md）：{offenders}"
    )
