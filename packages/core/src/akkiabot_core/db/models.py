"""ORM models (docs/architecture.md §5.3). Telegram ids are always BIGINT."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Enum, Index, Integer, MetaData, String, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from akkiabot_core.db.types import PortableJSON, TelegramId, UTCDateTime, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _str_enum[E: enum.Enum](cls: type[E], name: str) -> Enum:
    # Stored as VARCHAR + CHECK so adding a member never needs ALTER TYPE.
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class ChatSettings(BaseModel):
    """Per-chat feature switches, stored in ``chat.settings``."""

    model_config = ConfigDict(extra="ignore")

    antispam: bool = False
    captcha: bool = False
    warn: bool = False
    max_warn_count: int = Field(default=3, ge=1, le=100)
    delete_service_messages: bool = True


class Restriction(enum.StrEnum):
    NONE = "none"
    CAPTCHA_MUTE = "captcha_mute"
    SPAM_MUTE = "spam_mute"
    WARN_BAN = "warn_ban"
    ADMIN_MUTE = "admin_mute"
    ADMIN_BAN = "admin_ban"


class CaptchaStatus(enum.StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    EXPIRED = "expired"


class Chat(Base):
    __tablename__ = "chat"

    id: Mapped[int] = mapped_column(TelegramId, primary_key=True, autoincrement=False)
    type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(String(255))
    settings: Mapped[dict[str, Any]] = mapped_column(PortableJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    @property
    def chat_settings(self) -> ChatSettings:
        return ChatSettings.model_validate(self.settings or {})


class ChatMember(Base):
    __tablename__ = "chat_member"

    chat_id: Mapped[int] = mapped_column(TelegramId, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(TelegramId, primary_key=True, autoincrement=False)
    restriction: Mapped[Restriction] = mapped_column(
        _str_enum(Restriction, "restriction"), default=Restriction.NONE
    )
    restricted_by: Mapped[int | None] = mapped_column(TelegramId)
    restricted_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    warn_count: Mapped[int] = mapped_column(Integer, default=0)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class MessageLog(Base):
    __tablename__ = "message_log"
    __table_args__ = (
        Index("ix_message_log_chat_id_user_id", "chat_id", "user_id"),
        Index("ix_message_log_created_at", "created_at"),
    )

    chat_id: Mapped[int] = mapped_column(TelegramId, primary_key=True, autoincrement=False)
    message_id: Mapped[int] = mapped_column(TelegramId, primary_key=True, autoincrement=False)
    user_id: Mapped[int | None] = mapped_column(TelegramId)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    spam_verdict: Mapped[dict[str, Any] | None] = mapped_column(PortableJSON)
    text: Mapped[str | None] = mapped_column(Text)
    """Only stored for messages flagged as spam (audit)."""


class CaptchaChallenge(Base):
    __tablename__ = "captcha_challenge"
    __table_args__ = (
        Index("ix_captcha_challenge_chat_id_user_id", "chat_id", "user_id"),
        Index("ix_captcha_challenge_status_expires_at", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[int] = mapped_column(TelegramId)
    user_id: Mapped[int] = mapped_column(TelegramId)
    status: Mapped[CaptchaStatus] = mapped_column(
        _str_enum(CaptchaStatus, "captcha_status"), default=CaptchaStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    group_message_id: Mapped[int | None] = mapped_column(TelegramId)
