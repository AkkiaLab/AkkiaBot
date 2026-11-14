"""``akkiabot import-legacy`` against a mock botlab-core SQLite file.

The fixture is hand-built test data (mock): the DDL mirrors what GORM's
AutoMigrate creates for botlab-core's models, but every row is invented. It
verifies the mapping only; it is **not** an acceptance run on the real legacy
database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from akkiabot_core.db import Chat, ChatMember, ChatSettings, create_engine, create_session_factory
from akkiabot_core.db.migrate import upgrade
from akkiabot_server import legacy_import
from akkiabot_server.main import main

# DDL as emitted by gorm.io/driver/sqlite AutoMigrate for model.TelegramChat /
# model.TelegramChatMember (botlab-core 52c8b40), trimmed of indexes.
LEGACY_DDL = """
CREATE TABLE `telegram_chats` (
  `id` integer, `created_at` datetime, `updated_at` datetime, `deleted_at` datetime,
  `type` text, `title` text, `username` text, `bio` text, `description` text,
  `enable_captcha` numeric, `enable_anti_spam` numeric,
  `enable_warn` numeric DEFAULT false, `max_warn_count` integer DEFAULT 3,
  PRIMARY KEY (`id`)
);
CREATE TABLE `telegram_chat_members` (
  `id` integer, `created_at` datetime, `updated_at` datetime, `deleted_at` datetime,
  `chat_id` integer, `user_id` integer, `join_time` datetime,
  `message_count` integer DEFAULT 0, `last_message_time` datetime,
  `restriction_by` integer, `restriction_type` text, `restriction_until` datetime,
  `warn_count` integer DEFAULT 0,
  PRIMARY KEY (`id`)
);
CREATE UNIQUE INDEX `idx_chat_member` ON `telegram_chat_members`(`chat_id`,`user_id`);
"""

TS = "2025-01-20 12:00:00.000000+08:00"
G1, G2, G3, G_DELETED, PRIVATE = -1001, -1002, -1003, -1004, 42

# (id, type, title, captcha, antispam, warn, max_warn, deleted_at)
CHATS: list[tuple[Any, ...]] = [
    (G1, "supergroup", "Mock 群 | 一", 1, 1, 1, 3, None),
    (G2, "group", "Mock 群二", 0, 1, 0, 5, None),
    (G3, "supergroup", "Mock 群三", 1, 0, 1, 0, None),  # 0 is below the new minimum
    (G_DELETED, "supergroup", "已删除", 1, 1, 1, 3, TS),
    (PRIVATE, "private", "", 0, 0, 0, 3, None),
]

# (chat_id, user_id, warn_count, restriction_type, deleted_at)
MEMBERS: list[tuple[Any, ...]] = [
    (G1, 11, 2, "NoRestriction", None),
    (G1, 12, 0, "CaptchaBAN", None),  # still captcha-muted
    (G1, 13, 0, "AdminBAN", None),
    (G2, 21, 1, "", None),
    (G2, 22, 4, None, TS),  # soft-deleted
    (G3, 31, 1, "NoRestriction", None),  # >= clamped limit 1
    (G_DELETED, 41, 2, "CaptchaBAN", None),  # chat is gone
    (PRIVATE, 51, 1, None, None),  # not a group
]


def make_legacy_db(
    path: Path, chats: list[tuple[Any, ...]], members: list[tuple[Any, ...]]
) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(LEGACY_DDL)
        conn.executemany(
            "INSERT INTO telegram_chats (id, type, title, enable_captcha, enable_anti_spam, "
            "enable_warn, max_warn_count, deleted_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*row, TS, TS) for row in chats],
        )
        conn.executemany(
            "INSERT INTO telegram_chat_members (chat_id, user_id, warn_count, restriction_type, "
            "deleted_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(*row, TS, TS) for row in members],
        )
        conn.commit()
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    return make_legacy_db(tmp_path / "legacy.db", CHATS, MEMBERS)


@pytest.fixture
def target_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    upgrade(url)
    return url


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _import(url: str, path: Path, *, dry_run: bool) -> legacy_import.ImportPlan:
    engine = create_engine(url)
    try:
        snapshot = legacy_import.read_legacy(path)
        return await legacy_import.run_import(
            snapshot, create_session_factory(engine), dry_run=dry_run
        )
    finally:
        await engine.dispose()


async def _rows(url: str) -> tuple[dict[int, dict[str, Any]], dict[tuple[int, int], int]]:
    engine = create_engine(url)
    try:
        async with create_session_factory(engine)() as session:
            chats = {c.id: dict(c.settings) for c in (await session.scalars(select(Chat))).all()}
            members = {
                (m.chat_id, m.user_id): m.warn_count
                for m in (await session.scalars(select(ChatMember))).all()
            }
        return chats, members
    finally:
        await engine.dispose()


async def _seed(url: str) -> None:
    engine = create_engine(url)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            session.add(
                Chat(
                    id=G1,
                    type="supergroup",
                    title="新库里的标题",
                    settings=ChatSettings(antispam=True, delete_service_messages=False).model_dump(
                        mode="json"
                    ),
                )
            )
            session.add(ChatMember(chat_id=G2, user_id=21, warn_count=1, message_count=7))
    finally:
        await engine.dispose()


# ----------------------------------------------------------------------- reading


def test_read_legacy_ignores_soft_deleted_rows_and_counts_with_sql(legacy_db: Path) -> None:
    snapshot = legacy_import.read_legacy(legacy_db)

    assert [c.id for c in snapshot.chats] == sorted([G1, G2, G3, PRIVATE])
    assert all(m.user_id != 22 for m in snapshot.members)
    assert snapshot.stats == legacy_import.SourceStats(
        chats_total=4,
        chats_soft_deleted=1,
        group_chats=3,
        captcha_enabled=2,
        antispam_enabled=2,
        warn_enabled=2,
        members_total=7,
        members_with_warnings=3,
        warn_count_sum=4,
        captcha_muted=2,
    )


def test_read_legacy_never_modifies_the_file(legacy_db: Path, target_url: str) -> None:
    before = digest(legacy_db)
    run(_import(target_url, legacy_db, dry_run=False))
    assert digest(legacy_db) == before
    assert not Path(f"{legacy_db}-journal").exists()
    assert not Path(f"{legacy_db}-wal").exists()


def test_connection_is_read_only(legacy_db: Path) -> None:
    connect = legacy_import._connect_read_only  # pyright: ignore[reportPrivateUsage]
    with (
        closing(connect(legacy_db)) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute("DELETE FROM telegram_chats")


def test_missing_file_and_wrong_schema_are_reported(tmp_path: Path) -> None:
    with pytest.raises(legacy_import.LegacyImportError, match="不存在"):
        legacy_import.read_legacy(tmp_path / "nope.db")

    other = tmp_path / "other.db"
    with closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE telegram_chats (id integer, type text)")
    with pytest.raises(legacy_import.LegacyImportError) as exc:
        legacy_import.read_legacy(other)
    assert "telegram_chats.enable_captcha" in str(exc.value)
    assert "表 telegram_chat_members" in str(exc.value)

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a sqlite database at all" * 100)
    with pytest.raises(legacy_import.LegacyImportError):
        legacy_import.read_legacy(garbage)


def test_text_booleans_are_parsed(tmp_path: Path) -> None:
    path = make_legacy_db(
        tmp_path / "t.db", [(G1, "group", "t", "true", "false", "t", 3, None)], []
    )
    chat = legacy_import.read_legacy(path).chats[0]
    assert (chat.captcha, chat.antispam, chat.warn) == (True, False, True)

    bad = make_legacy_db(tmp_path / "b.db", [(G1, "group", "t", "yes", 0, 0, 3, None)], [])
    with pytest.raises(legacy_import.LegacyImportError, match="布尔值"):
        legacy_import.read_legacy(bad)


# ----------------------------------------------------------------------- plan


def test_plan_against_empty_target(legacy_db: Path) -> None:
    snapshot = legacy_import.read_legacy(legacy_db)
    plan = legacy_import.build_plan(
        snapshot, legacy_import.TargetState.empty(), target_checked=False
    )

    assert plan.consistent
    assert {c.chat_id: c.after for c in plan.chats} == {
        G1: {"captcha": True, "antispam": True, "warn": True, "max_warn_count": 3},
        G2: {"captcha": False, "antispam": True, "warn": False, "max_warn_count": 5},
        G3: {"captcha": True, "antispam": False, "warn": True, "max_warn_count": 1},
    }
    assert {c.action for c in plan.chats} == {"create"}
    assert [(m.chat_id, m.user_id, m.after) for m in plan.members] == [
        (G3, 31, 1),
        (G2, 21, 1),
        (G1, 11, 2),
    ]
    assert [(m.chat_id, m.user_id, m.chat_title) for m in plan.captcha_muted] == [
        (G_DELETED, 41, None),
        (G1, 12, "Mock 群 | 一"),
    ]
    assert plan.skipped_chats == {"private": 1}
    assert plan.skipped_members_unknown_chat == 2
    notes = "\n".join(plan.notes)
    assert "MaxWarnCount=0" in notes
    assert "用户 31 的警告数 1 已达到上限 1" in notes
    assert "未配置 DB_URL" in notes


def test_plan_mismatch_with_source_stats_is_inconsistent(legacy_db: Path) -> None:
    snapshot = legacy_import.read_legacy(legacy_db)
    tampered = legacy_import.LegacySnapshot(snapshot.chats[1:], snapshot.members, snapshot.stats)
    plan = legacy_import.build_plan(
        tampered, legacy_import.TargetState.empty(), target_checked=False
    )
    assert not plan.consistent
    assert "**不一致**" in legacy_import.render_report(plan, source=Path("x.db"), dry_run=True)


# ----------------------------------------------------------------------- apply


def test_dry_run_writes_nothing(legacy_db: Path, target_url: str) -> None:
    plan = run(_import(target_url, legacy_db, dry_run=True))
    assert plan.consistent
    assert plan.target_checked
    assert plan.counts() == {"chats": {"create": 3}, "members": {"create": 3}}
    assert run(_rows(target_url)) == ({}, {})


def test_import_writes_settings_and_warnings_and_is_idempotent(
    legacy_db: Path, target_url: str
) -> None:
    run(_seed(target_url))

    plan = run(_import(target_url, legacy_db, dry_run=False))
    assert plan.counts() == {
        "chats": {"update": 1, "create": 2},
        "members": {"create": 2, "unchanged": 1},
    }
    g1 = next(c for c in plan.chats if c.chat_id == G1)
    assert g1.before == {"captcha": False, "antispam": True, "warn": False, "max_warn_count": 3}

    chats, members = run(_rows(target_url))
    # Existing keys the legacy bot never had are kept.
    assert chats[G1] == {
        "antispam": True,
        "captcha": True,
        "warn": True,
        "max_warn_count": 3,
        "delete_service_messages": False,
    }
    assert ChatSettings.model_validate(chats[G3]).max_warn_count == 1
    assert ChatSettings.model_validate(chats[G2]).delete_service_messages is True
    assert members == {(G1, 11): 2, (G2, 21): 1, (G3, 31): 1}
    # Captcha-muted members are listed only, never written.
    assert (G1, 12) not in members

    again = run(_import(target_url, legacy_db, dry_run=False))
    assert again.counts() == {"chats": {"unchanged": 3}, "members": {"unchanged": 3}}


def test_new_chat_rows_keep_legacy_type_and_title(legacy_db: Path, target_url: str) -> None:
    run(_import(target_url, legacy_db, dry_run=False))

    async def fetch() -> tuple[str, str | None]:
        engine = create_engine(target_url)
        try:
            async with create_session_factory(engine)() as session:
                chat = await session.get(Chat, G2)
                assert chat is not None
                return chat.type, chat.title
        finally:
            await engine.dispose()

    assert run(fetch()) == ("group", "Mock 群二")


def test_import_requires_migrated_target(legacy_db: Path, tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    with pytest.raises(legacy_import.LegacyImportError, match="akkiabot migrate"):
        run(_import(url, legacy_db, dry_run=True))


def test_real_import_without_target_is_refused(legacy_db: Path) -> None:
    snapshot = legacy_import.read_legacy(legacy_db)
    with pytest.raises(legacy_import.LegacyImportError, match="DB_URL"):
        run(legacy_import.run_import(snapshot, None, dry_run=False))


# ----------------------------------------------------------------------- report & CLI


def test_report_lists_diff_and_captcha_members(legacy_db: Path) -> None:
    snapshot = legacy_import.read_legacy(legacy_db)
    plan = legacy_import.build_plan(
        snapshot, legacy_import.TargetState.empty(), target_checked=False
    )
    report = legacy_import.render_report(plan, source=legacy_db, dry_run=True)

    assert "模式：dry-run（未写入）" in report
    assert "| 群组数 | 3 | 3 | 一致 |" in report
    assert "| 警告数合计 | 4 | 4 | 一致 |" in report
    assert "| 群配置 chat.settings | 3 | 0 | 0 |" in report
    assert "Mock 群 \\| 一" in report  # pipes escaped in table cells
    assert "| -1001 | Mock 群 \\| 一 | 12 |" in report
    assert "跳过的非群组记录：private × 1" in report
    assert str(legacy_db.parent) not in report  # only the file name


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("cli_env")
def test_cli_dry_run_without_db_url(
    legacy_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "diff.json"
    assert main(["import-legacy", str(legacy_db), "--dry-run", "--report", str(report)]) == 0

    out, err = capsys.readouterr()
    assert out.startswith("# import-legacy 差异报告")
    assert "空库（未配置 DB_URL）" in out
    assert '"message": "legacy import planned"' in err
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["consistent"] is True
    assert data["source_stats"]["group_chats"] == 3
    assert data["counts"] == {"chats": {"create": 3}, "members": {"create": 3}}
    assert len(data["captcha_muted"]) == 2


@pytest.mark.usefixtures("cli_env")
def test_cli_import_with_db_url(
    legacy_db: Path,
    target_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DB_URL", target_url)
    assert main(["import-legacy", str(legacy_db)]) == 0
    out, err = capsys.readouterr()
    assert "模式：已写入" in out
    assert '"message": "legacy import applied"' in err
    chats, members = run(_rows(target_url))
    assert set(chats) == {G1, G2, G3}
    assert len(members) == 3


@pytest.mark.usefixtures("cli_env")
def test_cli_errors(legacy_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["import-legacy", str(legacy_db)]) == 2  # real import needs DB_URL
    assert main(["import-legacy", str(tmp_path / "missing.db"), "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "正式导入需要配置 DB_URL" in err
    assert "旧库文件不存在" in err


@pytest.mark.usefixtures("cli_env")
def test_cli_inconsistent_plan_exits_1_and_writes_nothing(
    legacy_db: Path, target_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_URL", target_url)
    original = legacy_import.build_plan

    def broken(*args: Any, **kwargs: Any) -> legacy_import.ImportPlan:
        plan = original(*args, **kwargs)
        plan.members.pop()
        return plan

    monkeypatch.setattr(legacy_import, "build_plan", broken)
    assert main(["import-legacy", str(legacy_db)]) == 1
    assert run(_rows(target_url)) == ({}, {})
