from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, override

from sqlalchemy import JSON, BigInteger, DateTime, Dialect, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB

# Telegram ids exceed 32 bits (supergroups are -100xxxxxxxxxx), always BIGINT.
TelegramId = BigInteger

# Portable JSON: JSONB on PostgreSQL, plain JSON elsewhere.
PortableJSON = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    SQLite has no timezone support and returns naive values; this type stores
    UTC and always hands back aware ``datetime`` objects.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime 不允许写入数据库，请使用带时区的 UTC 时间")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    @override
    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        assert isinstance(value, datetime)  # noqa: S101
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)
