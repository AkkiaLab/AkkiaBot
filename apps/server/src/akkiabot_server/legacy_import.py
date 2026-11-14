"""One-shot import of group settings and warning counts from the Go botlab-core.

``akkiabot import-legacy <sqlite.db>`` (docs/migration-plan.md M10,
docs/operations.md §9) reads a **read-only copy** of botlab-core's SQLite file
and maps:

* ``telegram_chats``: ``enable_captcha``/``enable_anti_spam``/``enable_warn``/
  ``max_warn_count`` → ``chat.settings`` (``captcha``/``antispam``/``warn``/
  ``max_warn_count``). Only group / supergroup chats are imported; other keys of
  an existing ``chat.settings`` (e.g. ``delete_service_messages``) are kept.
* ``telegram_chat_members.warn_count`` (> 0) → ``chat_member.warn_count``.

Members still muted by the legacy captcha (``restriction_type = 'CaptchaBAN'``)
are only listed: the new system has no challenge for them, so an admin handles
them by hand. Soft-deleted rows (``deleted_at IS NOT NULL``) are ignored, as
GORM does.

Column names follow GORM's naming of the legacy models
(``model/telegram_chat.go``, ``model/telegram_chat_member.go`` in botlab-core).
The source counts in the report come from independent SQL aggregates, so a
mismatch with the planned rows is a bug and aborts the import.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.db import Chat, ChatMember, ChatSettings
from akkiabot_core.db.types import utcnow

logger = logging.getLogger(__name__)

GROUP_TYPES = frozenset({"group", "supergroup"})
CAPTCHA_MUTE = "CaptchaBAN"
"""``model.CaptchaMute`` in botlab-core."""

# ChatSettings.max_warn_count bounds
_MIN_WARN, _MAX_WARN = 1, 100

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "telegram_chats": (
        "id",
        "deleted_at",
        "type",
        "title",
        "enable_captcha",
        "enable_anti_spam",
        "enable_warn",
        "max_warn_count",
    ),
    "telegram_chat_members": (
        "chat_id",
        "user_id",
        "deleted_at",
        "warn_count",
        "restriction_type",
        "updated_at",
    ),
}

SETTING_FIELDS = ("captcha", "antispam", "warn", "max_warn_count")

Action = Literal["create", "update", "unchanged"]


class LegacyImportError(Exception):
    """The legacy file cannot be read or does not look like botlab-core's database."""


# --------------------------------------------------------------------------- source


@dataclass(frozen=True)
class LegacyChat:
    id: int
    type: str
    title: str | None
    captcha: bool
    antispam: bool
    warn: bool
    max_warn_count: int


@dataclass(frozen=True)
class LegacyMember:
    chat_id: int
    user_id: int
    warn_count: int
    restriction_type: str | None
    updated_at: str | None


@dataclass(frozen=True)
class SourceStats:
    """Counted with SQL directly on the legacy file, independent of the mapping."""

    chats_total: int
    chats_soft_deleted: int
    group_chats: int
    captcha_enabled: int
    antispam_enabled: int
    warn_enabled: int
    members_total: int
    members_with_warnings: int
    """Live members with ``warn_count > 0`` in live group chats."""
    warn_count_sum: int
    captcha_muted: int
    """Live members with ``restriction_type = 'CaptchaBAN'`` (any chat)."""


@dataclass(frozen=True)
class LegacySnapshot:
    chats: list[LegacyChat]
    members: list[LegacyMember]
    stats: SourceStats


def _parse_bool(value: Any, *, where: str) -> bool:
    if value in (0, 1):  # GORM's sqlite driver stores bool as 0/1 (numeric)
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "t", "false", "f", "1", "0"}:
        return value.strip().lower() in {"true", "t", "1"}
    if value is None:
        return False
    raise LegacyImportError(f"无法解析的布尔值 {value!r}（{where}）")


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise LegacyImportError(f"旧库文件不存在：{path}")
    # mode=ro: SQLite refuses every write on this connection.
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        raise LegacyImportError(f"无法以只读方式打开旧库：{exc}") from exc
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise LegacyImportError(f"无法以只读方式打开旧库：{exc}") from exc
    return conn


def _check_schema(conn: sqlite3.Connection) -> None:
    missing: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            missing.append(f"表 {table}")
            continue
        missing.extend(f"{table}.{c}" for c in columns if c not in present)
    if missing:
        raise LegacyImportError("旧库结构与 botlab-core 不一致，缺少：" + "、".join(missing))


_SOURCE_STATS_SQL = """
WITH live_chats AS (SELECT * FROM telegram_chats WHERE deleted_at IS NULL),
     groups AS (SELECT * FROM live_chats WHERE type IN ('group', 'supergroup')),
     live_members AS (SELECT * FROM telegram_chat_members WHERE deleted_at IS NULL),
     warned AS (
       SELECT * FROM live_members
       WHERE warn_count > 0 AND chat_id IN (SELECT id FROM groups)
     )
SELECT
  (SELECT count(*) FROM live_chats),
  (SELECT count(*) FROM telegram_chats WHERE deleted_at IS NOT NULL),
  (SELECT count(*) FROM groups),
  (SELECT count(*) FROM groups WHERE enable_captcha IN (1, '1', 'true', 't', 'TRUE', 'T')),
  (SELECT count(*) FROM groups WHERE enable_anti_spam IN (1, '1', 'true', 't', 'TRUE', 'T')),
  (SELECT count(*) FROM groups WHERE enable_warn IN (1, '1', 'true', 't', 'TRUE', 'T')),
  (SELECT count(*) FROM live_members),
  (SELECT count(*) FROM warned),
  (SELECT coalesce(sum(warn_count), 0) FROM warned),
  (SELECT count(*) FROM live_members WHERE restriction_type = :captcha_mute)
"""


def _source_stats(conn: sqlite3.Connection) -> SourceStats:
    row = conn.execute(_SOURCE_STATS_SQL, {"captcha_mute": CAPTCHA_MUTE}).fetchone()
    return SourceStats(*(int(v) for v in row))


def read_legacy(path: Path) -> LegacySnapshot:
    """Read the live rows and source statistics from botlab-core's SQLite file."""
    with closing(_connect_read_only(path)) as conn:
        try:
            _check_schema(conn)
            chats = [
                LegacyChat(
                    id=int(row[0]),
                    type=str(row[1] or ""),
                    title=row[2],
                    captcha=_parse_bool(row[3], where=f"chat {row[0]} enable_captcha"),
                    antispam=_parse_bool(row[4], where=f"chat {row[0]} enable_anti_spam"),
                    warn=_parse_bool(row[5], where=f"chat {row[0]} enable_warn"),
                    max_warn_count=int(row[6] if row[6] is not None else 3),
                )
                for row in conn.execute(
                    "SELECT id, type, title, enable_captcha, enable_anti_spam, enable_warn, "
                    "max_warn_count FROM telegram_chats WHERE deleted_at IS NULL ORDER BY id"
                )
            ]
            members = [
                LegacyMember(
                    chat_id=int(row[0]),
                    user_id=int(row[1]),
                    warn_count=int(row[2] or 0),
                    restriction_type=row[3],
                    updated_at=None if row[4] is None else str(row[4]),
                )
                for row in conn.execute(
                    "SELECT chat_id, user_id, warn_count, restriction_type, updated_at "
                    "FROM telegram_chat_members WHERE deleted_at IS NULL "
                    "ORDER BY chat_id, user_id"
                )
            ]
            stats = _source_stats(conn)
        except sqlite3.DatabaseError as exc:
            raise LegacyImportError(f"读取旧库失败：{exc}") from exc
    return LegacySnapshot(chats, members, stats)


# --------------------------------------------------------------------------- plan


@dataclass(frozen=True)
class ChatChange:
    chat_id: int
    title: str | None
    action: Action
    before: dict[str, Any] | None
    """The four mapped settings currently in the target (``None`` when the chat is new)."""
    after: dict[str, Any]


@dataclass(frozen=True)
class MemberChange:
    chat_id: int
    user_id: int
    action: Action
    before: int | None
    after: int
    limit: int


@dataclass(frozen=True)
class CaptchaMuted:
    chat_id: int
    chat_title: str | None
    user_id: int
    since: str | None
    """``updated_at`` of the legacy row (raw text, legacy timezone)."""


@dataclass
class ImportPlan:
    source: SourceStats
    target_checked: bool
    """``False`` when the dry-run compared against an empty target (no ``DB_URL``)."""
    chats: list[ChatChange] = field(default_factory=list)
    members: list[MemberChange] = field(default_factory=list)
    captcha_muted: list[CaptchaMuted] = field(default_factory=list)
    skipped_chats: dict[str, int] = field(default_factory=dict)
    """Live non-group chats by legacy type."""
    skipped_members_unknown_chat: int = 0
    """Members with warnings whose chat is not an imported group."""
    notes: list[str] = field(default_factory=list)

    @property
    def checks(self) -> list[tuple[str, int, int]]:
        """``(name, source, planned)``; the import only runs when every pair matches."""
        src = self.source
        return [
            ("群组数", src.group_chats, len(self.chats)),
            ("开启入群验证的群", src.captcha_enabled, sum(c.after["captcha"] for c in self.chats)),
            ("开启反垃圾的群", src.antispam_enabled, sum(c.after["antispam"] for c in self.chats)),
            ("开启警告的群", src.warn_enabled, sum(c.after["warn"] for c in self.chats)),
            ("有警告的成员", src.members_with_warnings, len(self.members)),
            ("警告数合计", src.warn_count_sum, sum(m.after for m in self.members)),
            ("captcha 禁言中的成员", src.captcha_muted, len(self.captcha_muted)),
        ]

    @property
    def consistent(self) -> bool:
        return all(src == planned for _, src, planned in self.checks)

    def counts(self) -> dict[str, dict[str, int]]:
        return {
            "chats": dict(Counter(c.action for c in self.chats)),
            "members": dict(Counter(m.action for m in self.members)),
        }


@dataclass(frozen=True)
class TargetState:
    """What the new database already has for the chats / members in scope."""

    settings: dict[int, ChatSettings]
    warn_counts: dict[tuple[int, int], int]

    @classmethod
    def empty(cls) -> TargetState:
        return cls({}, {})


def _mapped(settings: ChatSettings) -> dict[str, Any]:
    return {name: getattr(settings, name) for name in SETTING_FIELDS}


def build_plan(
    snapshot: LegacySnapshot, target: TargetState, *, target_checked: bool
) -> ImportPlan:
    plan = ImportPlan(source=snapshot.stats, target_checked=target_checked)
    groups: dict[int, LegacyChat] = {}
    skipped: Counter[str] = Counter()
    for chat in snapshot.chats:
        if chat.type in GROUP_TYPES:
            groups[chat.id] = chat
        else:
            skipped[chat.type or "(空)"] += 1
    plan.skipped_chats = dict(sorted(skipped.items()))

    limits: dict[int, int] = {}
    for chat in groups.values():
        limit = min(max(chat.max_warn_count, _MIN_WARN), _MAX_WARN)
        if limit != chat.max_warn_count:
            plan.notes.append(
                f"群 {chat.id} 的 MaxWarnCount={chat.max_warn_count} 超出新系统范围 "
                f"{_MIN_WARN}–{_MAX_WARN}，按 {limit} 导入"
            )
        limits[chat.id] = limit
        existing = target.settings.get(chat.id)
        new = (existing or ChatSettings()).model_copy(
            update={
                "captcha": chat.captcha,
                "antispam": chat.antispam,
                "warn": chat.warn,
                "max_warn_count": limit,
            }
        )
        before = None if existing is None else _mapped(existing)
        after = _mapped(new)
        action: Action = (
            "create" if existing is None else ("unchanged" if before == after else "update")
        )
        plan.chats.append(ChatChange(chat.id, chat.title, action, before, after))

    for member in snapshot.members:
        if member.restriction_type == CAPTCHA_MUTE:
            title = groups[member.chat_id].title if member.chat_id in groups else None
            plan.captcha_muted.append(
                CaptchaMuted(member.chat_id, title, member.user_id, member.updated_at)
            )
        if member.warn_count <= 0:
            continue
        if member.chat_id not in groups:
            plan.skipped_members_unknown_chat += 1
            continue
        limit = limits[member.chat_id]
        before = target.warn_counts.get((member.chat_id, member.user_id))
        action = (
            "create"
            if before is None
            else ("unchanged" if before == member.warn_count else "update")
        )
        plan.members.append(
            MemberChange(member.chat_id, member.user_id, action, before, member.warn_count, limit)
        )
        if member.warn_count >= limit:
            plan.notes.append(
                f"群 {member.chat_id} 用户 {member.user_id} 的警告数 {member.warn_count} "
                f"已达到上限 {limit}，下一次 /warn 会直接封禁"
            )

    if plan.skipped_members_unknown_chat:
        plan.notes.append(
            f"{plan.skipped_members_unknown_chat} 个带警告的成员所在的群不在导入范围"
            "（非群组或群记录已删除），已跳过"
        )
    if not target_checked:
        plan.notes.append("未配置 DB_URL：差异按空的新库计算，不代表正式导入时的实际变更")

    return plan


# --------------------------------------------------------------------------- target


async def check_target_schema(session: AsyncSession) -> None:
    tables = await session.run_sync(lambda s: set(inspect(s.get_bind()).get_table_names()))
    if not {"chat", "chat_member"} <= tables:
        raise LegacyImportError("新库缺少 chat / chat_member 表，请先执行 akkiabot migrate")


async def load_target(session: AsyncSession, snapshot: LegacySnapshot) -> TargetState:
    chat_ids = [c.id for c in snapshot.chats if c.type in GROUP_TYPES]
    settings: dict[int, ChatSettings] = {}
    warn_counts: dict[tuple[int, int], int] = {}
    # Chunk the IN lists to stay below SQLite's bound-parameter limit.
    for i in range(0, len(chat_ids), 500):
        chunk = chat_ids[i : i + 500]
        for chat in await session.scalars(select(Chat).where(Chat.id.in_(chunk))):
            settings[chat.id] = chat.chat_settings
        rows = await session.execute(
            select(ChatMember.chat_id, ChatMember.user_id, ChatMember.warn_count).where(
                ChatMember.chat_id.in_(chunk)
            )
        )
        for chat_id, user_id, warn_count in rows:
            warn_counts[(chat_id, user_id)] = warn_count
    return TargetState(settings, warn_counts)


async def apply_plan(session: AsyncSession, plan: ImportPlan, snapshot: LegacySnapshot) -> None:
    """Write the planned changes. The caller owns the transaction."""
    legacy = {c.id: c for c in snapshot.chats}
    now = utcnow()
    for change in plan.chats:
        if change.action == "unchanged":
            continue
        chat = await session.get(Chat, change.chat_id)
        if chat is None:
            source = legacy[change.chat_id]
            settings = ChatSettings(**change.after)
            session.add(
                Chat(
                    id=change.chat_id,
                    type=source.type,
                    title=source.title,
                    settings=settings.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            # Keep keys the legacy bot never had (e.g. delete_service_messages).
            settings = chat.chat_settings.model_copy(update=change.after)
            chat.settings = settings.model_dump(mode="json")
            chat.updated_at = now
    for change in plan.members:
        if change.action == "unchanged":
            continue
        member = await session.get(ChatMember, (change.chat_id, change.user_id))
        if member is None:
            session.add(
                ChatMember(chat_id=change.chat_id, user_id=change.user_id, warn_count=change.after)
            )
        else:
            member.warn_count = change.after
    await session.flush()


async def run_import(
    snapshot: LegacySnapshot,
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    dry_run: bool,
) -> ImportPlan:
    """Plan against the target (or an empty one) and, unless ``dry_run``, apply in one transaction.

    The import is aborted (nothing written) when the plan disagrees with the
    source statistics.
    """
    if session_factory is None:
        if not dry_run:
            raise LegacyImportError("正式导入需要配置 DB_URL")
        plan = build_plan(snapshot, TargetState.empty(), target_checked=False)
        _log_plan(plan, dry_run=True)
        return plan

    # Plan and apply in the same transaction so the diff matches what is written.
    # Leaving the block without commit (dry-run / failed checks) rolls back.
    async with session_factory() as session, session.begin():
        await check_target_schema(session)
        plan = build_plan(snapshot, await load_target(session, snapshot), target_checked=True)
        _log_plan(plan, dry_run=dry_run)
        if dry_run or not plan.consistent:
            await session.rollback()
            return plan
        await apply_plan(session, plan, snapshot)
    logger.info("legacy import applied", extra=plan.counts())
    return plan


def _log_plan(plan: ImportPlan, *, dry_run: bool) -> None:
    logger.info(
        "legacy import planned",
        extra={
            "dry_run": dry_run,
            "consistent": plan.consistent,
            "captcha_muted": len(plan.captcha_muted),
            "notes": len(plan.notes),
            **plan.counts(),
        },
    )


# --------------------------------------------------------------------------- report

_ACTION_LABEL = {"create": "新建", "update": "更新", "unchanged": "不变"}


def _fmt_settings(values: dict[str, Any] | None) -> str:
    if values is None:
        return "（无）"
    return ", ".join(
        f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in values.items()
    )


def render_report(plan: ImportPlan, *, source: Path, dry_run: bool) -> str:
    """Human-readable diff report (Markdown)."""
    src = plan.source
    counts = plan.counts()
    mode = (
        "dry-run（未写入）" if dry_run else ("已写入" if plan.consistent else "未写入（校验失败）")
    )
    lines = [
        "# import-legacy 差异报告",
        "",
        f"- 旧库：`{source.name}`（只读打开）",
        f"- 模式：{mode}",
        f"- 对比目标：{'新库（DB_URL）' if plan.target_checked else '空库（未配置 DB_URL）'}",
        "",
        "## 源数据统计（直接 SQL 计数）",
        "",
        "| 项目 | 数量 |",
        "| --- | --- |",
        f"| 群记录（未删除） | {src.chats_total} |",
        f"| 群记录（软删除，忽略） | {src.chats_soft_deleted} |",
        f"| 其中 group / supergroup | {src.group_chats} |",
        f"| 开启入群验证 | {src.captcha_enabled} |",
        f"| 开启反垃圾 | {src.antispam_enabled} |",
        f"| 开启警告 | {src.warn_enabled} |",
        f"| 成员记录（未删除） | {src.members_total} |",
        f"| 群组中有警告的成员 | {src.members_with_warnings} |",
        f"| 警告数合计 | {src.warn_count_sum} |",
        f"| captcha 禁言中的成员 | {src.captcha_muted} |",
        "",
        "## 一致性校验（源数据 vs 导入计划）",
        "",
        "| 项目 | 源数据 | 导入计划 | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {name} | {s} | {p} | {'一致' if s == p else '**不一致**'} |"
        for name, s, p in plan.checks
    ]
    lines += [
        "",
        "## 变更汇总",
        "",
        "| 对象 | 新建 | 更新 | 不变 |",
        "| --- | --- | --- | --- |",
    ]
    for key, label in (("chats", "群配置 chat.settings"), ("members", "警告数 chat_member")):
        c = counts[key]
        lines.append(
            f"| {label} | {c.get('create', 0)} | {c.get('update', 0)} | {c.get('unchanged', 0)} |"
        )
    if plan.skipped_chats:
        skipped = "、".join(f"{t} × {n}" for t, n in plan.skipped_chats.items())
        lines += ["", f"跳过的非群组记录：{skipped}"]

    lines += ["", "## 群配置差异", ""]
    changed = [c for c in plan.chats if c.action != "unchanged"]
    if changed:
        lines += ["| 群 id | 标题 | 动作 | 原值 | 新值 |", "| --- | --- | --- | --- | --- |"]
        lines += [
            f"| {c.chat_id} | {_md_cell(c.title)} | {_ACTION_LABEL[c.action]} "
            f"| {_fmt_settings(c.before)} | {_fmt_settings(c.after)} |"
            for c in changed
        ]
    else:
        lines.append("无变更。")

    lines += ["", "## 警告数差异", ""]
    changed_members = [m for m in plan.members if m.action != "unchanged"]
    if changed_members:
        lines += [
            "| 群 id | 用户 id | 动作 | 原值 | 新值 | 上限 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {m.chat_id} | {m.user_id} | {_ACTION_LABEL[m.action]} "
            f"| {'（无）' if m.before is None else m.before} | {m.after} | {m.limit} |"
            for m in changed_members
        ]
    else:
        lines.append("无变更。")

    lines += ["", "## 需要管理员手动处理：captcha 禁言中的成员", ""]
    if plan.captcha_muted:
        lines += [
            "新系统没有这些成员对应的验证，导入不会改动他们。请管理员在客户端中逐个确认："
            "是真人就解除限制，否则移出。",
            "",
            "| 群 id | 群标题 | 用户 id | 旧库更新时间 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {m.chat_id} | {_md_cell(m.chat_title)} | {m.user_id} | {m.since or ''} |"
            for m in plan.captcha_muted
        ]
    else:
        lines.append("无。")

    if plan.notes:
        lines += ["", "## 注意事项", ""]
        lines += [f"- {note}" for note in plan.notes]
    return "\n".join(lines) + "\n"


def _md_cell(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ")


def plan_to_json(plan: ImportPlan, *, source: Path, dry_run: bool) -> dict[str, Any]:
    return {
        "source": source.name,
        "dry_run": dry_run,
        "target_checked": plan.target_checked,
        "consistent": plan.consistent,
        "source_stats": asdict(plan.source),
        "checks": [{"name": n, "source": s, "planned": p} for n, s, p in plan.checks],
        "counts": plan.counts(),
        "skipped_chats": plan.skipped_chats,
        "skipped_members_unknown_chat": plan.skipped_members_unknown_chat,
        "chats": [asdict(c) for c in plan.chats],
        "members": [asdict(m) for m in plan.members],
        "captcha_muted": [asdict(m) for m in plan.captcha_muted],
        "notes": plan.notes,
    }
