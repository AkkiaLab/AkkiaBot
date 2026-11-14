"""Token / session files, op wrapper, login, CLI skip path and the managed server helpers."""

from __future__ import annotations

import contextlib
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from akkiabot_e2e import cli
from akkiabot_e2e.errors import ConfigError, E2EError, SafetyError
from akkiabot_e2e.files import read_private_file, write_private_file
from akkiabot_e2e.login import Ask, interactive_login
from akkiabot_e2e.onepassword import (
    TOKEN_ENV,
    op_command,
    op_environment,
    token_init,
)
from akkiabot_e2e.server import ManagedServer, query_message_log, server_env
from akkiabot_e2e.testing import ADMIN, BOT, MOCK_ADMIN_SESSION, live_env
from akkiabot_e2e.transport import UserInfo

# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_OP_TOKEN = "ops_synthetic-service-account-token"
# mock: 人工构造的不可用测试值，不符合真实凭据格式，不是凭据。
MOCK_OP_TOKEN_OVERRIDE = "ops_x"
# mock: 故意畸形的不可用 bot token。
MOCK_MALFORMED_BOT_TOKEN = "1:x"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_private_file_is_owner_only_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "secret"
    write_private_file(path, "value")
    assert mode(path) == 0o600
    assert mode(path.parent) == 0o700
    with pytest.raises(SafetyError, match="--force"):
        write_private_file(path, "other")
    write_private_file(path, "other", overwrite=True)
    assert read_private_file(path) == "other"


def test_read_private_file_refuses_wide_permissions(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(MOCK_OP_TOKEN)
    path.chmod(0o644)
    with pytest.raises(SafetyError, match="chmod 600"):
        read_private_file(path)


def test_token_init_validates_and_stores(tmp_path: Path) -> None:
    path = tmp_path / "op-token"
    with pytest.raises(ConfigError, match="ops_"):
        token_init(path, lambda _: "not-a-token")
    assert not path.exists()
    token_init(path, lambda _: f"  {MOCK_OP_TOKEN}\n")
    assert read_private_file(path) == MOCK_OP_TOKEN
    assert mode(path) == 0o600


def test_op_token_goes_through_env_not_argv(tmp_path: Path) -> None:
    path = tmp_path / "op-token"
    token_init(path, lambda _: MOCK_OP_TOKEN)
    env, note = op_environment({"PATH": "/bin"}, path)
    command = op_command(Path("tools/e2e/staging.env.tpl"), ["akkiabot-e2e", "run"])

    assert env[TOKEN_ENV] == MOCK_OP_TOKEN
    assert MOCK_OP_TOKEN not in " ".join(command)
    assert MOCK_OP_TOKEN not in note
    assert command == [
        "op",
        "run",
        "--env-file=tools/e2e/staging.env.tpl",
        "--",
        "akkiabot-e2e",
        "run",
    ]
    # An explicitly exported token wins; with neither, op's own sign-in is used.
    assert (
        op_environment({TOKEN_ENV: MOCK_OP_TOKEN_OVERRIDE}, path)[0][TOKEN_ENV]
        == MOCK_OP_TOKEN_OVERRIDE
    )
    assert TOKEN_ENV not in op_environment({}, tmp_path / "missing")[0]
    with pytest.raises(ConfigError):
        op_command(Path("x"), [])


class FakeLogin:
    def __init__(self, user: UserInfo) -> None:
        self.user = user
        self.closed = False
        self.asked: list[str] = []

    async def login(self, phone: Ask, code: Ask, password: Ask) -> UserInfo:
        self.asked += [phone(), code(), password()]
        return self.user

    def session_string(self) -> str:
        return MOCK_ADMIN_SESSION

    async def close(self) -> None:
        self.closed = True


async def test_login_returns_session_in_memory_without_printing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeLogin(ADMIN)
    user, session = await interactive_login(
        client, prompt=lambda _: "+10000000000", secret_prompt=lambda _: "2fa"
    )
    assert user == ADMIN
    assert session.reveal() == MOCK_ADMIN_SESSION
    assert MOCK_ADMIN_SESSION not in repr(session)
    assert client.closed
    assert MOCK_ADMIN_SESSION not in capsys.readouterr().out


async def test_login_refuses_bot() -> None:
    client = FakeLogin(BOT)
    with pytest.raises(SafetyError, match="bot"):
        await interactive_login(client, prompt=str, secret_prompt=str)
    assert client.closed


def test_login_out_refuses_existing_file_before_contacting_telegram(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.write_text("old")
    code = cli.main(["login", "--role", "admin", "--out", str(existing)], env=live_env())
    assert code == cli.EXIT_USAGE
    assert existing.read_text() == "old"


def test_run_without_credentials_is_skipped_not_passed(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run"], env={"E2E_API_ID": "1"})
    out = capsys.readouterr().out
    assert code == cli.EXIT_SKIPPED
    assert "已跳过" in out
    assert "不是通过" in out
    assert "E2E_ADMIN_SESSION" in out
    assert "E2E_API_ID," not in out


def test_run_rejects_unknown_scenario_before_connecting(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "--scenarios", "m9"], env=live_env())
    assert code == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "m9" in err
    assert MOCK_ADMIN_SESSION not in err


def test_list_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"], env={}) == 0
    assert "m3.ip-reply" in capsys.readouterr().out


def test_server_env_drops_user_sessions_and_op_token(tmp_path: Path) -> None:
    base = {
        **live_env(),
        TOKEN_ENV: MOCK_OP_TOKEN,
        "TELEGRAM_BOT_TOKEN": MOCK_MALFORMED_BOT_TOKEN,
        "DB_URL": "postgres://prod",
    }
    env = server_env(base, db_path=tmp_path / "bot.db", port=1, metrics_port=2)
    assert not [k for k in env if k.startswith(("E2E_", "OP_"))]
    assert env["TELEGRAM_BOT_TOKEN"] == MOCK_MALFORMED_BOT_TOKEN
    assert env["DB_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    assert env["SERVER_HOST"] == env["OBSERVABILITY_METRICS_HOST"] == "127.0.0.1"
    assert env["OBSERVABILITY_SENTRY_ENVIRONMENT"] == "staging"
    # The bot under test is pinned to the test environment, whatever the caller set.
    assert env["TELEGRAM_TEST_ENVIRONMENT"] == "true"
    forced = server_env(
        {"TELEGRAM_TEST_ENVIRONMENT": "false"}, db_path=tmp_path / "x", port=1, metrics_port=2
    )
    assert forced["TELEGRAM_TEST_ENVIRONMENT"] == "true"


def test_query_message_log(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE message_log (chat_id INTEGER, message_id INTEGER)")
        conn.executemany("INSERT INTO message_log VALUES (?, ?)", [(-100, 1), (-100, 2), (-200, 3)])
    assert query_message_log(db, -100, [1, 3, 4]) == {1}
    assert query_message_log(db, -100, []) == set()


FAKE_SERVER = """
import http.server, os, sys
if sys.argv[1] == "migrate":
    print("migrated", os.environ["DB_URL"])
    sys.exit(int(os.environ.get("FAKE_MIGRATE_EXIT", "0")))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/readyz" else 404)
        self.end_headers()
    def log_message(self, *args):
        pass
print("serving, has session:", any(k.startswith("E2E_") for k in os.environ), flush=True)
http.server.HTTPServer(("127.0.0.1", int(os.environ["SERVER_PORT"])), H).serve_forever()
"""


async def test_managed_server_starts_waits_ready_and_stops(tmp_path: Path) -> None:
    script = tmp_path / "fake_akkiabot.py"
    script.write_text(FAKE_SERVER)
    server = ManagedServer(tmp_path, live_env(), command=[sys.executable, str(script)])

    async with server:
        process = server._process
        assert process is not None
        assert process.poll() is None
    assert process.poll() is not None
    log = server.log_path.read_text()
    assert "migrated sqlite+aiosqlite" in log
    assert "has session: False" in log
    assert mode(server.log_path) == 0o600


async def test_managed_server_reports_failed_migration(tmp_path: Path) -> None:
    script = tmp_path / "fake_akkiabot.py"
    script.write_text(FAKE_SERVER)
    env = {**live_env(), "FAKE_MIGRATE_EXIT": "3"}
    with pytest.raises(E2EError, match="退出码 3"):
        async with ManagedServer(tmp_path, env, command=[sys.executable, str(script)]):
            pass
