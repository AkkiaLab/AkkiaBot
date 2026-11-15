"""One-shot import of group settings and member state from the Go botlab-core.

``akkiabot import-legacy <sqlite.db>`` (docs/migration-plan.md M10,
docs/operations.md §9) reads a **read-only copy** of botlab-core's SQLite file
and maps:

* ``telegram_chats``: ``enable_captcha``/``enable_anti_spam``/``enable_warn``/
  ``max_warn_count`` → ``chat.settings`` (``captcha``/``antispam``/``warn``/
  ``max_warn_count``). Only group / supergroup chats are imported; other keys of
  an existing ``chat.settings`` (e.g. ``delete_service_messages``) are kept.
* ``telegram_chat_members.warn_count`` (> 0) → ``chat_member.warn_count``.
  Member state is imported for those groups and for *unregistered* groups:
  negative chat ids that have members or messages but no ``telegram_chats``
  row (botlab-core only saved a chat on ``/config``; such groups run with every
  switch off in both systems, so no ``chat`` row is created for them).
* ``telegram_chat_members.message_count`` (> 0) → ``chat_member.message_count``
  (the larger of both values is kept). Both systems exempt members with more
  than 3 messages from the spam check, so this keeps regulars exempt.
* ``telegram_messages`` of the last ``recent_hours`` (default 48, Telegram's
  limit for bots deleting messages) → ``message_log`` ids, without text, so a
  ban right after the cutover still purges the member's recent messages.

Members still muted by the legacy captcha (``restriction_type = 'CaptchaBAN'``)
are not written. They stay muted in Telegram, exactly as before the cutover
(owner's decision in BOT-16); the report counts them per chat and lists only
those muted within ``recent_hours`` before the legacy bot stopped, whose old
verification button no longer works. The full list is in the JSON report.

Other legacy restrictions (``AdminBAN``/``AdminSPAM``) are not imported either:
Telegram keeps the real ban, while a stale record could skip the captcha for
a member an admin has since unbanned.

Soft-deleted rows (``deleted_at IS NOT NULL``) are ignored, as GORM does. Time
windows are measured from the legacy database's last activity (newest message
or member update), not from the wall clock.

Column names follow GORM's naming of the legacy models
(``model/telegram_chat.go``, ``model/telegram_chat_member.go``,
``model/telegram_message.go`` in botlab-core).
The source counts in the report come from independent SQL aggregates, so a
mismatch with the planned rows is a bug and aborts the import.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import insert, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from akkiabot_core.db import Chat, ChatMember, ChatSettings, MessageLog
from akkiabot_core.db.types import utcnow

logger = logging.getLogger(__name__)

GROUP_TYPES = frozenset({"group", "supergroup"})
CAPTCHA_MUTE = "CaptchaBAN"
"""``model.CaptchaMute`` in botlab-core."""

# ChatSettings.max_warn_count bounds
_MIN_WARN, _MAX_WARN = 1, 100
DEFAULT_RECENT_HOURS = 48
_BATCH = 500

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
        "message_count",
        "restriction_type",
        "updated_at",
    ),
    "telegram_messages": ("chat_id", "user_id", "message_id", "deleted_at", "created_at"),
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
    message_count: int
    restriction_type: str | None
    updated_at: str | None


@dataclass(frozen=True)
class LegacyMessage:
    chat_id: int
    user_id: int
    message_id: int
    created_at: datetime


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
    """Live members with ``warn_count > 0`` in live or unregistered group chats."""
    warn_count_sum: int
    members_with_messages: int
    """Live members with ``message_count > 0`` in live or unregistered group chats."""
    message_count_sum: int
    captcha_muted: int
    """Live members with ``restriction_type = 'CaptchaBAN'`` (any chat)."""


@dataclass(frozen=True)
class LegacySnapshot:
    chats: list[LegacyChat]
    members: list[LegacyMember]
    stats: SourceStats
    recent_messages: list[LegacyMessage] = field(default_factory=list)
    """Live messages in group chats within ``recent_hours`` of ``last_activity``."""
    last_activity: datetime | None = None
    recent_hours: int = DEFAULT_RECENT_HOURS
    unregistered_groups: frozenset[int] = frozenset()
    """Negative chat ids with members or messages but no ``telegram_chats`` row."""


_UNREGISTERED_SQL = """
SELECT chat_id FROM telegram_chat_members
WHERE deleted_at IS NULL AND chat_id < 0 AND chat_id NOT IN (SELECT id FROM telegram_chats)
UNION
SELECT chat_id FROM telegram_messages
WHERE deleted_at IS NULL AND chat_id < 0 AND chat_id NOT IN (SELECT id FROM telegram_chats)
"""


_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse GORM's SQLite timestamps (nanosecond fractions, offset); naive means UTC."""
    if value is None:
        return None
    match = _TS.match(str(value).strip())
    if match is None:
        return None
    date, time, fraction, offset = match.groups()
    text = f"{date}T{time}"
    if fraction:
        text += "." + fraction[:6].ljust(6, "0")
    if offset and offset != "Z":
        text += offset if ":" in offset else f"{offset[:3]}:{offset[3:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_bool(value: Any, *, where: str) -> bool:
    if value in (0, 1):  # GORM's sqlite driver stores bool as 0/1 (numeric)
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "t", "false", "f", "1", "0"}:
        return value.strip().lower() in {"true", "t", "1"}
    if value is None:
        return False
    raise LegacyImportError(f"无法解析的布尔值 {value!r}（{where}）")


def check_report_path(report: Path, legacy_db: Path) -> None:
    """Refuse a ``--report`` path that exists or could alias the legacy database.

    Only new files are written, so the same path, a symlink and a hard link to
    the legacy file are all rejected (they exist); a dangling symlink is
    rejected too, because writing through it would create its target.
    """
    if report.is_symlink():
        raise LegacyImportError(f"报告路径不能是符号链接：{report}")
    if report.exists():
        if legacy_db.exists() and report.samefile(legacy_db):
            raise LegacyImportError("报告路径指向旧库文件本身，拒绝覆盖")
        raise LegacyImportError(f"报告文件已存在，拒绝覆盖：{report}")
    if report.resolve() == legacy_db.resolve():
        raise LegacyImportError("报告路径指向旧库文件本身，拒绝覆盖")
    if not report.parent.is_dir():
        raise LegacyImportError(f"报告所在目录不存在：{report.parent}")


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
     importable AS (
       SELECT * FROM live_members
       WHERE chat_id IN (SELECT id FROM groups)
          OR (chat_id < 0 AND chat_id NOT IN (SELECT id FROM telegram_chats))
     ),
     warned AS (SELECT * FROM importable WHERE warn_count > 0),
     talked AS (SELECT * FROM importable WHERE message_count > 0)
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
  (SELECT count(*) FROM talked),
  (SELECT coalesce(sum(message_count), 0) FROM talked),
  (SELECT count(*) FROM live_members WHERE restriction_type = :captcha_mute)
"""


def _source_stats(conn: sqlite3.Connection) -> SourceStats:
    row = conn.execute(_SOURCE_STATS_SQL, {"captcha_mute": CAPTCHA_MUTE}).fetchone()
    return SourceStats(*(int(v) for v in row))


def _recent_messages(
    conn: sqlite3.Connection, groups: set[int], last_activity: datetime | None, hours: int
) -> tuple[list[LegacyMessage], datetime | None]:
    """Messages within ``hours`` before the last legacy activity, and that moment.

    ``id`` grows with time (autoincrement), so the scan walks backwards and
    stops a day past the window instead of parsing the whole table.
    """
    rows = conn.execute(
        "SELECT chat_id, user_id, message_id, created_at FROM telegram_messages "
        "WHERE deleted_at IS NULL ORDER BY id DESC"
    )
    reference = last_activity
    cutoff: datetime | None = None
    messages: list[LegacyMessage] = []
    for chat_id, user_id, message_id, created_at in rows:
        at = parse_timestamp(created_at)
        if at is None:
            continue
        if cutoff is None:  # newest message
            reference = at if reference is None else max(reference, at)
            cutoff = reference - timedelta(hours=hours)
        if at < cutoff - timedelta(days=1):
            break
        if at >= cutoff and chat_id in groups and user_id is not None and message_id is not None:
            messages.append(LegacyMessage(int(chat_id), int(user_id), int(message_id), at))
    messages.sort(key=lambda m: (m.chat_id, m.message_id))
    return messages, reference


def read_legacy(path: Path, *, recent_hours: int = DEFAULT_RECENT_HOURS) -> LegacySnapshot:
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
                    message_count=int(row[3] or 0),
                    restriction_type=row[4],
                    updated_at=None if row[5] is None else str(row[5]),
                )
                for row in conn.execute(
                    "SELECT chat_id, user_id, warn_count, message_count, restriction_type, "
                    "updated_at "
                    "FROM telegram_chat_members WHERE deleted_at IS NULL "
                    "ORDER BY chat_id, user_id"
                )
            ]
            stats = _source_stats(conn)
            member_times = [t for m in members if (t := parse_timestamp(m.updated_at))]
            unregistered = frozenset(int(r[0]) for r in conn.execute(_UNREGISTERED_SQL))
            groups = {c.id for c in chats if c.type in GROUP_TYPES} | unregistered
            recent, last_activity = _recent_messages(
                conn, groups, max(member_times, default=None), recent_hours
            )
        except sqlite3.DatabaseError as exc:
            raise LegacyImportError(f"读取旧库失败：{exc}") from exc
    return LegacySnapshot(chats, members, stats, recent, last_activity, recent_hours, unregistered)


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
    legacy_warn: int
    legacy_messages: int
    warn_before: int | None
    """``None`` when the target has no row."""
    warn_after: int
    messages_before: int | None
    messages_after: int
    limit: int

    @property
    def warn_changed(self) -> bool:
        return self.warn_before != self.warn_after and self.legacy_warn > 0


@dataclass(frozen=True)
class MessageChange:
    chat_id: int
    message_id: int
    user_id: int
    created_at: datetime
    action: Literal["create", "unchanged"]


@dataclass(frozen=True)
class CaptchaMuted:
    chat_id: int
    chat_title: str | None
    user_id: int
    since: str | None
    """``updated_at`` of the legacy row (raw text)."""
    recent: bool
    """Muted within ``recent_hours`` before the legacy bot stopped (or time unknown)."""


@dataclass
class ImportPlan:
    source: SourceStats
    target_checked: bool
    """``False`` when the dry-run compared against an empty target (no ``DB_URL``)."""
    recent_hours: int = DEFAULT_RECENT_HOURS
    last_activity: datetime | None = None
    chats: list[ChatChange] = field(default_factory=list)
    members: list[MemberChange] = field(default_factory=list)
    messages: list[MessageChange] = field(default_factory=list)
    captcha_muted: list[CaptchaMuted] = field(default_factory=list)
    skipped_chats: dict[str, int] = field(default_factory=dict)
    """Live non-group chats by legacy type."""
    skipped_members_unknown_chat: int = 0
    """Members with warnings or messages whose chat is not an imported group."""
    unregistered_groups: int = 0
    """Groups without a legacy chat row whose members / messages are imported."""
    notes: list[str] = field(default_factory=list)

    @property
    def checks(self) -> list[tuple[str, int, int]]:
        """``(name, source, planned)``; the import only runs when every pair matches."""
        src = self.source
        warned = [m for m in self.members if m.legacy_warn > 0]
        talked = [m for m in self.members if m.legacy_messages > 0]
        return [
            ("群组数", src.group_chats, len(self.chats)),
            ("开启入群验证的群", src.captcha_enabled, sum(c.after["captcha"] for c in self.chats)),
            ("开启反垃圾的群", src.antispam_enabled, sum(c.after["antispam"] for c in self.chats)),
            ("开启警告的群", src.warn_enabled, sum(c.after["warn"] for c in self.chats)),
            ("有警告的成员", src.members_with_warnings, len(warned)),
            ("警告数合计", src.warn_count_sum, sum(m.legacy_warn for m in warned)),
            ("有发言数的成员", src.members_with_messages, len(talked)),
            ("发言数合计", src.message_count_sum, sum(m.legacy_messages for m in talked)),
            ("captcha 禁言中的成员", src.captcha_muted, len(self.captcha_muted)),
        ]

    @property
    def consistent(self) -> bool:
        return all(src == planned for _, src, planned in self.checks)

    def counts(self) -> dict[str, dict[str, int]]:
        return {
            "chats": dict(Counter(c.action for c in self.chats)),
            "members": dict(Counter(m.action for m in self.members)),
            "messages": dict(Counter(m.action for m in self.messages)),
        }


@dataclass(frozen=True)
class TargetState:
    """What the new database already has for the chats / members / messages in scope."""

    settings: dict[int, ChatSettings]
    members: dict[tuple[int, int], tuple[int, int]]
    """``(chat_id, user_id) → (warn_count, message_count)``."""
    messages: set[tuple[int, int]] = field(default_factory=set)
    """``(chat_id, message_id)`` already in ``message_log``."""

    @classmethod
    def empty(cls) -> TargetState:
        return cls({}, {}, set())


def _mapped(settings: ChatSettings) -> dict[str, Any]:
    return {name: getattr(settings, name) for name in SETTING_FIELDS}


def build_plan(
    snapshot: LegacySnapshot, target: TargetState, *, target_checked: bool
) -> ImportPlan:
    plan = ImportPlan(
        source=snapshot.stats,
        target_checked=target_checked,
        recent_hours=snapshot.recent_hours,
        last_activity=snapshot.last_activity,
        unregistered_groups=len(snapshot.unregistered_groups),
    )
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

    # Unregistered groups run with the defaults (warnings off, limit 3) in both systems.
    default_limit = ChatSettings().max_warn_count
    limits.update(dict.fromkeys(snapshot.unregistered_groups, default_limit))
    recent_since = (
        snapshot.last_activity - timedelta(hours=snapshot.recent_hours)
        if snapshot.last_activity
        else None
    )
    for member in snapshot.members:
        if member.restriction_type == CAPTCHA_MUTE:
            title = groups[member.chat_id].title if member.chat_id in groups else None
            at = parse_timestamp(member.updated_at)
            recent = at is None or recent_since is None or at >= recent_since
            plan.captcha_muted.append(
                CaptchaMuted(member.chat_id, title, member.user_id, member.updated_at, recent)
            )
        if member.warn_count <= 0 and member.message_count <= 0:
            continue
        if member.chat_id not in limits:
            plan.skipped_members_unknown_chat += 1
            continue
        limit = limits[member.chat_id]
        current = target.members.get((member.chat_id, member.user_id))
        warn_before, messages_before = current if current else (None, None)
        # Legacy warnings are authoritative; message counts only ever grow.
        warn_after = member.warn_count if member.warn_count > 0 else (warn_before or 0)
        messages_after = max(member.message_count, messages_before or 0)
        if current is None:
            action = "create"
        elif (warn_after, messages_after) == current:
            action = "unchanged"
        else:
            action = "update"
        plan.members.append(
            MemberChange(
                chat_id=member.chat_id,
                user_id=member.user_id,
                action=action,
                legacy_warn=max(member.warn_count, 0),
                legacy_messages=max(member.message_count, 0),
                warn_before=warn_before,
                warn_after=warn_after,
                messages_before=messages_before,
                messages_after=messages_after,
                limit=limit,
            )
        )
        if member.warn_count >= limit:
            plan.notes.append(
                f"群 {member.chat_id} 用户 {member.user_id} 的警告数 {member.warn_count} "
                f"已达到上限 {limit}，下一次 /warn 会直接封禁"
            )

    for message in snapshot.recent_messages:
        exists = (message.chat_id, message.message_id) in target.messages
        plan.messages.append(
            MessageChange(
                message.chat_id,
                message.message_id,
                message.user_id,
                message.created_at,
                "unchanged" if exists else "create",
            )
        )

    if plan.unregistered_groups:
        plan.notes.append(
            f"{plan.unregistered_groups} 个群在旧库中没有群记录（从未执行过 /config，所有开关为关），"
            "只导入其成员发言数、警告数和最近消息 id，不创建群配置；新系统中它们同样按默认（全关）运行"
        )
    if plan.skipped_members_unknown_chat:
        plan.notes.append(
            f"{plan.skipped_members_unknown_chat} 个有警告或发言数的成员所在的群不在导入范围"
            "（非群组或群记录已删除），已跳过"
        )
    if not target_checked:
        plan.notes.append("未配置 DB_URL：差异按空的新库计算，不代表正式导入时的实际变更")

    return plan


# --------------------------------------------------------------------------- target


async def check_target_schema(session: AsyncSession) -> None:
    tables = await session.run_sync(lambda s: set(inspect(s.get_bind()).get_table_names()))
    if not {"chat", "chat_member", "message_log"} <= tables:
        raise LegacyImportError(
            "新库缺少 chat / chat_member / message_log 表，请先执行 akkiabot migrate"
        )


async def load_target(session: AsyncSession, snapshot: LegacySnapshot) -> TargetState:
    chat_ids = sorted(
        {c.id for c in snapshot.chats if c.type in GROUP_TYPES} | snapshot.unregistered_groups
    )
    settings: dict[int, ChatSettings] = {}
    members: dict[tuple[int, int], tuple[int, int]] = {}
    messages: set[tuple[int, int]] = set()
    # Chunk the IN lists to stay below SQLite's bound-parameter limit.
    for i in range(0, len(chat_ids), _BATCH):
        chunk = chat_ids[i : i + _BATCH]
        for chat in await session.scalars(select(Chat).where(Chat.id.in_(chunk))):
            settings[chat.id] = chat.chat_settings
        rows = await session.execute(
            select(
                ChatMember.chat_id,
                ChatMember.user_id,
                ChatMember.warn_count,
                ChatMember.message_count,
            ).where(ChatMember.chat_id.in_(chunk))
        )
        for chat_id, user_id, warn_count, message_count in rows:
            members[(chat_id, user_id)] = (warn_count, message_count)
    by_chat: dict[int, list[int]] = {}
    for message in snapshot.recent_messages:
        by_chat.setdefault(message.chat_id, []).append(message.message_id)
    for chat_id, ids in by_chat.items():
        for i in range(0, len(ids), _BATCH):
            found = await session.scalars(
                select(MessageLog.message_id).where(
                    MessageLog.chat_id == chat_id, MessageLog.message_id.in_(ids[i : i + _BATCH])
                )
            )
            messages.update((chat_id, m) for m in found)
    return TargetState(settings, members, messages)


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
    await session.flush()

    # Tens of thousands of members: bulk-insert new rows, update the rest one by one.
    new_members = [
        {
            "chat_id": m.chat_id,
            "user_id": m.user_id,
            "warn_count": m.warn_after,
            "message_count": m.messages_after,
        }
        for m in plan.members
        if m.action == "create"
    ]
    for i in range(0, len(new_members), _BATCH):
        await session.execute(insert(ChatMember), new_members[i : i + _BATCH])
    for change in plan.members:
        if change.action != "update":
            continue
        member = await session.get(ChatMember, (change.chat_id, change.user_id))
        assert member is not None  # noqa: S101 - planned from the same transaction
        member.warn_count = change.warn_after
        member.message_count = change.messages_after

    new_messages = [
        {
            "chat_id": m.chat_id,
            "message_id": m.message_id,
            "user_id": m.user_id,
            "created_at": m.created_at,
        }
        for m in plan.messages
        if m.action == "create"
    ]
    for i in range(0, len(new_messages), _BATCH):
        await session.execute(insert(MessageLog), new_messages[i : i + _BATCH])
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
            "captcha_muted_recent": sum(m.recent for m in plan.captcha_muted),
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
        f"- 旧库最后活动时间：{_fmt_time(plan.last_activity)}；“最近”指此前 {plan.recent_hours} 小时",
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
        f"| 群组中有发言数的成员 | {src.members_with_messages} |",
        f"| 发言数合计 | {src.message_count_sum} |",
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
    for key, label in (
        ("chats", "群配置 chat.settings"),
        ("members", "成员警告数 / 发言数 chat_member"),
        ("messages", "最近消息 id message_log"),
    ):
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
    changed_members = [m for m in plan.members if m.action != "unchanged" and m.warn_changed]
    if changed_members:
        lines += [
            "| 群 id | 用户 id | 动作 | 原值 | 新值 | 上限 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {m.chat_id} | {m.user_id} | {_ACTION_LABEL[m.action]} "
            f"| {'（无）' if m.warn_before is None else m.warn_before} | {m.warn_after} "
            f"| {m.limit} |"
            for m in changed_members
        ]
    else:
        lines.append("无变更。")

    message_changes = [m for m in plan.members if m.legacy_messages > 0 and m.action != "unchanged"]
    exempt = sum(m.messages_after > _EXEMPT_DEFAULT for m in plan.members)
    lines += [
        "",
        "## 发言数",
        "",
        f"写入或更新 {len(message_changes)} 名成员的发言数（取新旧两边的较大值）。"
        f"导入后发言数超过 {_EXEMPT_DEFAULT} 的成员共 {exempt} 名，"
        "按默认配置免于反垃圾检查，与旧版一致。逐人明细见 JSON 报告。",
    ]

    lines += ["", "## captcha 禁言中的成员（不导入）", ""]
    if plan.captcha_muted:
        recent = [m for m in plan.captcha_muted if m.recent]
        per_chat: dict[int, list[CaptchaMuted]] = {}
        for m in plan.captcha_muted:
            per_chat.setdefault(m.chat_id, []).append(m)
        lines += [
            "这些成员在 Telegram 中保持禁言，与切换前相同，导入不改动他们（负责人于 BOT-16 决定）。"
            "真人被卡住时可联系管理员解除。按群汇总如下，完整名单见 JSON 报告。",
            "",
            f"| 群 id | 群标题 | 禁言人数 | 其中最近 {plan.recent_hours} 小时 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {chat_id} | {_md_cell(items[0].chat_title)} | {len(items)} "
            f"| {sum(m.recent for m in items)} |"
            for chat_id, items in sorted(per_chat.items(), key=lambda kv: -len(kv[1]))
        ]
        lines += [
            "",
            f"### 最近 {plan.recent_hours} 小时内被禁言的成员（{len(recent)} 人）",
            "",
            "他们可能正在验证中途，旧的验证按钮在切换后失效。请管理员视情况在客户端中解除限制或移出。",
            "",
        ]
        if recent:
            lines += ["| 群 id | 群标题 | 用户 id | 旧库更新时间 |", "| --- | --- | --- | --- |"]
            lines += [
                f"| {m.chat_id} | {_md_cell(m.chat_title)} | {m.user_id} | {m.since or ''} |"
                for m in recent
            ]
        else:
            lines.append("无。")
    else:
        lines.append("无。")

    if plan.notes:
        lines += ["", "## 注意事项", ""]
        lines += [f"- {note}" for note in plan.notes]
    return "\n".join(lines) + "\n"


_EXEMPT_DEFAULT = 3
"""Default ``ANTISPAM_EXEMPT_MESSAGE_COUNT`` (botlab-core hard-coded the same 3)."""


def _fmt_time(value: datetime | None) -> str:
    return "未知" if value is None else value.strftime("%Y-%m-%d %H:%M:%S UTC")


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
        "recent_hours": plan.recent_hours,
        "last_activity": None if plan.last_activity is None else plan.last_activity.isoformat(),
        "source_stats": asdict(plan.source),
        "checks": [{"name": n, "source": s, "planned": p} for n, s, p in plan.checks],
        "counts": plan.counts(),
        "skipped_chats": plan.skipped_chats,
        "skipped_members_unknown_chat": plan.skipped_members_unknown_chat,
        "chats": [asdict(c) for c in plan.chats],
        "members": [asdict(m) for m in plan.members],
        "messages": [{**asdict(m), "created_at": m.created_at.isoformat()} for m in plan.messages],
        "captcha_muted": [asdict(m) for m in plan.captcha_muted],
        "notes": plan.notes,
    }
