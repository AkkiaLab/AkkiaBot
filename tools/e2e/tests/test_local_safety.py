"""Token / session files, op wrapper, login, CLI skip path and the managed server helpers."""

from __future__ import annotations

import contextlib
import functools
import json
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from akkiabot_e2e import cli, onepassword
from akkiabot_e2e.errors import ConfigError, E2EError, SafetyError
from akkiabot_e2e.files import read_private_file, write_private_file
from akkiabot_e2e.login import Ask, interactive_login
from akkiabot_e2e.onepassword import (
    TOKEN_ENV,
    op_command,
    op_environment,
    store_session,
    token_init,
)
from akkiabot_e2e.server import ManagedServer, query_message_log, server_env
from akkiabot_e2e.testing import ADMIN, ADMIN_SESSION, BOT, live_env
from akkiabot_e2e.transport import UserInfo

TOKEN = "ops_synthetic-service-account-token"


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
    path.write_text(TOKEN)
    path.chmod(0o644)
    with pytest.raises(SafetyError, match="chmod 600"):
        read_private_file(path)


def test_token_init_validates_and_stores(tmp_path: Path) -> None:
    path = tmp_path / "op-token"
    with pytest.raises(ConfigError, match="ops_"):
        token_init(path, lambda _: "not-a-token")
    assert not path.exists()
    token_init(path, lambda _: f"  {TOKEN}\n")
    assert read_private_file(path) == TOKEN
    assert mode(path) == 0o600


def test_op_token_goes_through_env_not_argv(tmp_path: Path) -> None:
    path = tmp_path / "op-token"
    token_init(path, lambda _: TOKEN)
    env, note = op_environment({"PATH": "/bin"}, path)
    command = op_command(Path("tools/e2e/staging.env.tpl"), ["akkiabot-e2e", "run"])

    assert env[TOKEN_ENV] == TOKEN
    assert TOKEN not in " ".join(command)
    assert TOKEN not in note
    assert command == [
        "op",
        "run",
        "--env-file=tools/e2e/staging.env.tpl",
        "--",
        "akkiabot-e2e",
        "run",
    ]
    # An explicitly exported token wins; with neither, op's own sign-in is used.
    assert op_environment({TOKEN_ENV: "ops_x"}, path)[0][TOKEN_ENV] == "ops_x"
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
        return ADMIN_SESSION

    async def close(self) -> None:
        self.closed = True


async def test_login_writes_session_privately_without_printing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, out = FakeLogin(ADMIN), tmp_path / "admin.session"
    user = await interactive_login(
        client, out, prompt=lambda _: "+10000000000", secret_prompt=lambda _: "2fa"
    )
    assert user == ADMIN
    assert read_private_file(out) == ADMIN_SESSION
    assert mode(out) == 0o600
    assert client.closed
    assert ADMIN_SESSION not in capsys.readouterr().out


async def test_login_refuses_bot_and_existing_file(tmp_path: Path) -> None:
    with pytest.raises(SafetyError, match="bot"):
        await interactive_login(FakeLogin(BOT), tmp_path / "s", prompt=str, secret_prompt=str)
    assert not (tmp_path / "s").exists()

    existing = tmp_path / "existing"
    existing.write_text("old")
    client = FakeLogin(ADMIN)
    with pytest.raises(SafetyError, match="--force"):
        await interactive_login(client, existing, prompt=str, secret_prompt=str)
    assert client.asked == []  # no login code was requested


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
    assert ADMIN_SESSION not in err


def test_list_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["list"], env={}) == 0
    assert "m3.ip-reply" in capsys.readouterr().out


def test_server_env_drops_user_sessions_and_op_token(tmp_path: Path) -> None:
    base = {
        **live_env(),
        TOKEN_ENV: TOKEN,
        "TELEGRAM_BOT_TOKEN": "1:x",
        "DB_URL": "postgres://prod",
    }
    env = server_env(base, db_path=tmp_path / "bot.db", port=1, metrics_port=2)
    assert not [k for k in env if k.startswith(("E2E_", "OP_"))]
    assert env["TELEGRAM_BOT_TOKEN"] == "1:x"
    assert env["DB_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    assert env["SERVER_HOST"] == env["OBSERVABILITY_METRICS_HOST"] == "127.0.0.1"
    assert env["OBSERVABILITY_SENTRY_ENVIRONMENT"] == "staging"


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


class FakeOp:
    """Records op invocations; stores the item JSON in memory."""

    def __init__(self, labels: list[str], *, fail: str | None = None, readback: str | None = None):
        self.item = {
            "id": "x",
            "fields": [
                {"id": f"f{i}", "label": label, "type": "STRING"} for i, label in enumerate(labels)
            ],
        }
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail = fail
        self.readback = readback

    def __call__(self, argv, stdin):
        argv = list(argv)
        self.calls.append((argv, stdin))
        verb = argv[1] if argv[1] == "read" else argv[2]
        if verb == self.fail:
            return 1, b"", f"[ERROR] boom {ADMIN_SESSION}".encode()
        if verb == "get":
            return 0, json.dumps(self.item).encode(), b""
        if verb == "edit":
            self.item = json.loads(stdin)
            return 0, b"item printed", b""
        value = next(
            f["value"] for f in self.item["fields"] if f["label"] == argv[2].rsplit("/", 1)[1]
        )
        return 0, (self.readback or value).encode() + b"\n", b""


def test_store_session_pipes_value_via_stdin_only() -> None:
    op = FakeOp(["api_id", "admin_session", "member_session"])

    store_session(
        ADMIN_SESSION, vault="AkkiaBot-Staging", item="telegram-e2e", field="admin_session", run=op
    )

    assert [argv[:3] for argv, _ in op.calls] == [
        ["op", "item", "get"],
        ["op", "item", "edit"],
        ["op", "read", "op://AkkiaBot-Staging/telegram-e2e/admin_session"],
    ]
    for argv, _ in op.calls:
        assert ADMIN_SESSION not in " ".join(argv)
    assert ADMIN_SESSION in (op.calls[1][1] or b"").decode()
    field = next(f for f in op.item["fields"] if f["label"] == "admin_session")
    assert field == {
        "id": "f1",
        "label": "admin_session",
        "type": "CONCEALED",
        "value": ADMIN_SESSION,
    }
    assert "value" not in op.item["fields"][2]  # other fields untouched


@pytest.mark.parametrize(
    ("op", "message"),
    [
        (FakeOp(["api_id"]), "找到 0 个"),
        (FakeOp(["admin_session"], fail="edit"), "写入 1Password"),
        (FakeOp(["admin_session"], fail="get"), "读取 1Password"),
        (FakeOp(["admin_session"], readback="other"), "不一致"),
    ],
)
def test_store_session_errors_are_redacted(op: FakeOp, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as info:
        store_session(ADMIN_SESSION, vault="v", item="i", field="admin_session", run=op)
    assert ADMIN_SESSION not in str(info.value)


def test_session_store_cli_deletes_file_only_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session_file = tmp_path / "admin.session"
    write_private_file(session_file, ADMIN_SESSION + "\n")
    op = FakeOp(["admin_session"], fail="edit")
    monkeypatch.setattr(onepassword, "run_op", op)
    monkeypatch.setattr(cli, "store_session", functools.partial(store_session, run=op))

    args = ["session-store", "--role", "admin", "--session-file", str(session_file)]
    assert cli.main(args, env={}) == cli.EXIT_USAGE
    assert session_file.exists()

    op.fail = None
    assert cli.main(args, env={}) == cli.EXIT_PASSED
    assert not session_file.exists()
    out = capsys.readouterr()
    assert ADMIN_SESSION not in out.out + out.err
