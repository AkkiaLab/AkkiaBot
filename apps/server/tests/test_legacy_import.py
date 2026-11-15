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

from akkiabot_core.db import (
    Chat,
    ChatMember,
    ChatSettings,
    MessageLog,
    create_engine,
    create_session_factory,
)
from akkiabot_core.db.migrate import upgrade
from akkiabot_server import legacy_import
from akkiabot_server.main import main

# DDL as emitted by gorm.io/driver/sqlite AutoMigrate for model.TelegramChat /
# model.TelegramChatMember / model.TelegramMessage (botlab-core 52c8b40), trimmed
# of indexes.
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
CREATE TABLE `telegram_messages` (
  `id` integer PRIMARY KEY AUTOINCREMENT, `created_at` datetime, `updated_at` datetime,
  `deleted_at` datetime, `chat_id` integer, `user_id` integer, `text` text,
  `message_id` integer, `spam_flag` integer DEFAULT 0
);
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

# (chat_id, user_id, warn_count, restriction_type, deleted_at[, message_count[, updated_at]])
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
    path: Path,
    chats: list[tuple[Any, ...]],
    members: list[tuple[Any, ...]],
    messages: list[tuple[Any, ...]] = (),  # pyright: ignore[reportArgumentType]
) -> Path:
    """``messages``: ``(chat_id, user_id, message_id, created_at, deleted_at)`` in id order."""
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
            "deleted_at, message_count, updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(*(row + (0, TS)[len(row) - 5 :]), TS) for row in members],
        )
        conn.executemany(
            "INSERT INTO telegram_messages (chat_id, user_id, message_id, created_at, deleted_at, "
            "text) VALUES (?, ?, ?, ?, ?, 'mock text')",
            list(messages),
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
        members_with_messages=0,
        message_count_sum=0,
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
    assert [(m.chat_id, m.user_id, m.warn_after) for m in plan.members] == [
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
    assert plan.counts() == {"chats": {"create": 3}, "members": {"create": 3}, "messages": {}}
    assert run(_rows(target_url)) == ({}, {})


def test_import_writes_settings_and_warnings_and_is_idempotent(
    legacy_db: Path, target_url: str
) -> None:
    run(_seed(target_url))

    plan = run(_import(target_url, legacy_db, dry_run=False))
    assert plan.counts() == {
        "chats": {"update": 1, "create": 2},
        "members": {"create": 2, "unchanged": 1},
        "messages": {},
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
    assert again.counts() == {
        "chats": {"unchanged": 3},
        "members": {"unchanged": 3},
        "messages": {},
    }


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
    assert data["counts"] == {"chats": {"create": 3}, "members": {"create": 3}, "messages": {}}
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


def _hardlink(src: Path, dst: Path) -> Path:
    dst.hardlink_to(src)
    return dst


def _symlink(src: Path, dst: Path) -> Path:
    dst.symlink_to(src)
    return dst


@pytest.mark.usefixtures("cli_env")
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    ("alias", "message"),
    [
        pytest.param(lambda src, d: src, "旧库文件本身", id="same-path"),
        pytest.param(
            lambda src, d: d / ".." / d.name / src.name, "旧库文件本身", id="relative-path"
        ),
        pytest.param(lambda src, d: _symlink(src, d / "link.json"), "符号链接", id="symlink"),
        pytest.param(lambda src, d: _hardlink(src, d / "hard.json"), "旧库文件本身", id="hardlink"),
        pytest.param(
            lambda src, d: _symlink(d / "missing.db", d / "dangling.json"),
            "符号链接",
            id="dangling-symlink",
        ),
    ],
)
def test_cli_report_never_overwrites_legacy_db(
    legacy_db: Path,
    target_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    alias: Any,
    message: str,
    dry_run: bool,
) -> None:
    monkeypatch.setenv("DB_URL", target_url)
    report = alias(legacy_db, tmp_path)
    before = digest(legacy_db)
    argv = ["import-legacy", str(legacy_db), "--report", str(report)]

    assert main([*argv, "--dry-run"] if dry_run else argv) == 2

    out, err = capsys.readouterr()
    assert out == ""  # rejected before reading anything
    assert message in err
    assert digest(legacy_db) == before
    assert run(_rows(target_url)) == ({}, {})
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.usefixtures("cli_env")
def test_cli_report_refuses_existing_file_and_missing_dir(
    legacy_db: Path,
    target_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DB_URL", target_url)
    existing = tmp_path / "old.json"
    existing.write_text("keep me", encoding="utf-8")

    assert main(["import-legacy", str(legacy_db), "--report", str(existing)]) == 2
    assert main(["import-legacy", str(legacy_db), "--report", str(tmp_path / "no" / "r.json")]) == 2

    err = capsys.readouterr().err
    assert "报告文件已存在" in err
    assert "报告所在目录不存在" in err
    assert existing.read_text(encoding="utf-8") == "keep me"
    assert run(_rows(target_url)) == ({}, {})


# ----------------------------------------------------------------------- member state & recent window

LAST = "2026-11-15 16:00:00+08:00"  # 08:00 UTC: newest legacy message = last activity

# Message counts, captcha mutes at different ages, messages around the 48 h window.
TIMED_MEMBERS: list[tuple[Any, ...]] = [
    (G1, 11, 0, "", None, 10, "2026-11-15 07:00:00.123456789+00:00"),
    (G1, 12, 0, "CaptchaBAN", None, 0, "2026-11-14 10:00:00+00:00"),  # recent
    (G1, 13, 0, "CaptchaBAN", None, 0, "2026-10-01 10:00:00+00:00"),  # old
    (G1, 14, 2, "", None, 3, "2026-11-01 10:00:00+00:00"),
    (G2, 21, 0, "", None, 0, "2026-11-01 10:00:00+00:00"),  # nothing to import
]
# (chat_id, user_id, message_id, created_at, deleted_at), oldest first (id order)
TIMED_MESSAGES: list[tuple[Any, ...]] = [
    (G1, 11, 100, "2026-11-10 10:00:00+00:00", None),  # too old
    (G1, 11, 101, "2026-11-13 09:00:00.123456789+00:00", None),
    (G1, 11, 102, "2026-11-14 09:00:00+00:00", TS),  # soft-deleted
    (PRIVATE, 51, 7, "2026-11-14 09:00:00+00:00", None),  # not a group
    (G1, None, 104, "2026-11-14 09:00:00+00:00", None),  # no sender
    (G2, 21, 105, LAST, None),
]


@pytest.fixture
def timed_db(tmp_path: Path) -> Path:
    return make_legacy_db(tmp_path / "timed.db", CHATS[:2], TIMED_MEMBERS, TIMED_MESSAGES)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-15 08:15:24.287366611+00:00", "2026-11-15T08:15:24.287366+00:00"),
        ("2025-01-20 12:00:00+08:00", "2025-01-20T04:00:00+00:00"),
        ("2025-01-20T12:00:00Z", "2025-01-20T12:00:00+00:00"),
        ("2025-01-20 12:00:00", "2025-01-20T12:00:00+00:00"),
        ("2025-01-20 12:00:00.5+0800", "2025-01-20T04:00:00.500000+00:00"),
    ],
)
def test_parse_timestamp(raw: str, expected: str) -> None:
    parsed = legacy_import.parse_timestamp(raw)
    assert parsed is not None
    assert parsed.isoformat() == expected


@pytest.mark.parametrize("raw", [None, "", "yesterday", "2025-13-40 99:00:00"])
def test_parse_timestamp_rejects_garbage(raw: str | None) -> None:
    assert legacy_import.parse_timestamp(raw) is None


def test_recent_window_is_measured_from_last_legacy_activity(timed_db: Path) -> None:
    snapshot = legacy_import.read_legacy(timed_db)

    assert snapshot.last_activity is not None
    assert snapshot.last_activity.isoformat() == "2026-11-15T08:00:00+00:00"
    assert [(m.chat_id, m.message_id, m.user_id) for m in snapshot.recent_messages] == [
        (G2, 105, 21),
        (G1, 101, 11),
    ]
    assert snapshot.stats.members_with_messages == 2
    assert snapshot.stats.message_count_sum == 13

    narrow = legacy_import.read_legacy(timed_db, recent_hours=1)
    assert [m.message_id for m in narrow.recent_messages] == [105]


def test_plan_imports_message_counts_and_flags_recent_captcha(timed_db: Path) -> None:
    snapshot = legacy_import.read_legacy(timed_db)
    plan = legacy_import.build_plan(
        snapshot, legacy_import.TargetState.empty(), target_checked=False
    )

    assert plan.consistent
    assert [(m.user_id, m.warn_after, m.messages_after) for m in plan.members] == [
        (11, 0, 10),
        (14, 2, 3),
    ]
    assert [(m.user_id, m.recent) for m in plan.captcha_muted] == [(12, True), (13, False)]
    assert plan.counts()["messages"] == {"create": 2}

    report = legacy_import.render_report(plan, source=timed_db, dry_run=True)
    assert "| 有发言数的成员 | 2 | 2 | 一致 |" in report
    assert "| 发言数合计 | 13 | 13 | 一致 |" in report
    assert "旧库最后活动时间：2026-11-15 08:00:00 UTC" in report
    assert "| -1001 | Mock 群 \\| 一 | 2 | 1 |" in report  # per-chat summary
    assert "最近 48 小时内被禁言的成员（1 人）" in report
    assert "| -1001 | Mock 群 \\| 一 | 12 |" in report
    assert "| -1001 | Mock 群 \\| 一 | 13 |" not in report  # old mutes: summary only
    assert "发言数超过 3 的成员共 1 名" in report

    data = legacy_import.plan_to_json(plan, source=timed_db, dry_run=True)
    assert [m["user_id"] for m in data["captcha_muted"]] == [12, 13]  # full list
    assert data["last_activity"] == "2026-11-15T08:00:00+00:00"
    assert data["messages"][0]["created_at"] == "2026-11-15T08:00:00+00:00"


def test_import_writes_message_counts_and_recent_message_ids(
    timed_db: Path, target_url: str
) -> None:
    async def seed() -> None:
        engine = create_engine(target_url)
        try:
            async with create_session_factory(engine)() as session, session.begin():
                # The new bot already counted more messages: the larger value wins.
                session.add(ChatMember(chat_id=G1, user_id=11, message_count=20))
        finally:
            await engine.dispose()

    async def message_log() -> list[tuple[int, int, int | None]]:
        engine = create_engine(target_url)
        try:
            async with create_session_factory(engine)() as session:
                rows = await session.execute(
                    select(MessageLog.chat_id, MessageLog.message_id, MessageLog.user_id)
                    .where(MessageLog.text.is_(None))
                    .order_by(MessageLog.message_id)
                )
                return [tuple(r) for r in rows]  # pyright: ignore[reportReturnType]
        finally:
            await engine.dispose()

    async def member_counts() -> dict[tuple[int, int], tuple[int, int, str]]:
        engine = create_engine(target_url)
        try:
            async with create_session_factory(engine)() as session:
                return {
                    (m.chat_id, m.user_id): (m.warn_count, m.message_count, m.restriction.value)
                    for m in (await session.scalars(select(ChatMember))).all()
                }
        finally:
            await engine.dispose()

    run(seed())
    plan = run(_import(target_url, timed_db, dry_run=False))
    assert plan.counts()["members"] == {"unchanged": 1, "create": 1}
    assert run(member_counts()) == {(G1, 11): (0, 20, "none"), (G1, 14): (2, 3, "none")}
    assert run(message_log()) == [(G1, 101, 11), (G2, 105, 21)]

    again = run(_import(target_url, timed_db, dry_run=False))
    assert again.counts() == {
        "chats": {"unchanged": 2},
        "members": {"unchanged": 2},
        "messages": {"unchanged": 2},
    }


@pytest.mark.usefixtures("cli_env")
def test_cli_recent_hours(timed_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["import-legacy", str(timed_db), "--dry-run", "--recent-hours", "0"])
    assert exc.value.code == 2
    capsys.readouterr()

    assert main(["import-legacy", str(timed_db), "--dry-run", "--recent-hours", "1"]) == 0
    assert "最近 1 小时内被禁言的成员（0 人）" in capsys.readouterr().out


UNREGISTERED = -1009


def test_unregistered_groups_get_member_state_but_no_chat_row(
    tmp_path: Path, target_url: str
) -> None:
    members = [
        *TIMED_MEMBERS,
        (UNREGISTERED, 91, 1, "", None, 8, "2026-11-15 07:00:00+00:00"),
        (77, 92, 0, "", None, 5, "2026-11-15 07:00:00+00:00"),  # private chat: skipped
    ]
    messages = [
        *TIMED_MESSAGES,
        (UNREGISTERED, 91, 300, "2026-11-15 07:30:00+00:00", None),
        (77, 92, 301, "2026-11-15 07:30:00+00:00", None),
    ]
    path = make_legacy_db(tmp_path / "u.db", CHATS[:2], members, messages)

    snapshot = legacy_import.read_legacy(path)
    assert snapshot.unregistered_groups == frozenset({UNREGISTERED})
    assert snapshot.stats.members_with_messages == 3
    assert snapshot.stats.members_with_warnings == 2

    plan = run(_import(target_url, path, dry_run=False))
    assert plan.consistent
    assert plan.skipped_members_unknown_chat == 1
    assert any("1 个群在旧库中没有群记录" in n for n in plan.notes)

    chats, member_rows = run(_rows(target_url))
    assert UNREGISTERED not in chats
    assert member_rows[(UNREGISTERED, 91)] == 1  # warn count
    assert (77, 92) not in member_rows
    assert {(m.chat_id, m.message_id) for m in plan.messages} >= {(UNREGISTERED, 300)}
    assert (77, 301) not in {(m.chat_id, m.message_id) for m in plan.messages}
