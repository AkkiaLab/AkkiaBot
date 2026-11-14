import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

import akkiabot_antispam
import akkiabot_core
import akkiabot_server
import akkiabot_whois
from akkiabot_server.main import main


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_server_depends_on_all_packages() -> None:
    versions = {
        m.__version__ for m in (akkiabot_server, akkiabot_core, akkiabot_antispam, akkiabot_whois)
    }
    assert versions == {"0.1.0"}


def test_main_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out == "akkiabot 0.1.0\n"


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == "akkiabot 0.1.0\n"


def test_migrate_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "bot.db"
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)  # not needed for migrate
    monkeypatch.chdir(tmp_path)

    assert main(["migrate"]) == 0

    with closing(sqlite3.connect(db)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"chat", "chat_member", "message_log", "captcha_challenge"} <= tables
    assert version == ("0001",)
    assert '"message": "migrations complete"' in capsys.readouterr().out


def test_migrate_rejects_bad_db_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DB_URL", "mysql://root:hunter2@db/bot")
    monkeypatch.chdir(tmp_path)
    assert main(["migrate"]) == 2
    out = capsys.readouterr().out
    assert "invalid database configuration" in out
    assert "hunter2" not in out


def test_serve_rejects_missing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for key in ("TELEGRAM_BOT_TOKEN", "DB_URL"):
        monkeypatch.delenv(key, raising=False)
    assert main(["serve"]) == 2
    assert "invalid configuration" in capsys.readouterr().out


def test_serve_runs_uvicorn_with_configured_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn
    from fastapi import FastAPI

    token = "123456:" + "B" * 35
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    monkeypatch.setenv("SERVER_HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER_PORT", "18080")
    monkeypatch.setenv("SERVER_SOFTWARE", "unrelated/1.0")  # foreign SERVER_* vars are ignored
    started: list[uvicorn.Config] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, *a, **k: started.append(self.config))

    assert main(["serve"]) == 0

    (config,) = started
    assert (config.host, config.port, config.log_config) == ("127.0.0.1", 18080, None)
    assert isinstance(config.app, FastAPI)
