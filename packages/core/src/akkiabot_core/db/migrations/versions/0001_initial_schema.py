"""initial schema: chat, chat_member, message_log, captcha_challenge

Revision ID: 0001
Revises:
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
tz_datetime = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "chat",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("settings", json_type, nullable=False),
        sa.Column("created_at", tz_datetime, nullable=False),
        sa.Column("updated_at", tz_datetime, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat")),
    )
    op.create_table(
        "chat_member",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "restriction",
            sa.Enum(
                "none",
                "captcha_mute",
                "spam_mute",
                "warn_ban",
                "admin_mute",
                "admin_ban",
                name="restriction",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("restricted_by", sa.BigInteger(), nullable=True),
        sa.Column("restricted_until", tz_datetime, nullable=True),
        sa.Column("warn_count", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("joined_at", tz_datetime, nullable=True),
        sa.Column("last_message_at", tz_datetime, nullable=True),
        sa.PrimaryKeyConstraint("chat_id", "user_id", name=op.f("pk_chat_member")),
    )
    op.create_table(
        "message_log",
        sa.Column("chat_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("message_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", tz_datetime, nullable=False),
        sa.Column("spam_verdict", json_type, nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("chat_id", "message_id", name=op.f("pk_message_log")),
    )
    op.create_index("ix_message_log_chat_id_user_id", "message_log", ["chat_id", "user_id"])
    op.create_index("ix_message_log_created_at", "message_log", ["created_at"])
    op.create_table(
        "captcha_challenge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "passed",
                "failed",
                "expired",
                name="captcha_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("created_at", tz_datetime, nullable=False),
        sa.Column("expires_at", tz_datetime, nullable=False),
        sa.Column("group_message_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_captcha_challenge")),
    )
    op.create_index(
        "ix_captcha_challenge_chat_id_user_id", "captcha_challenge", ["chat_id", "user_id"]
    )
    op.create_index(
        "ix_captcha_challenge_status_expires_at", "captcha_challenge", ["status", "expires_at"]
    )


def downgrade() -> None:
    op.drop_table("captcha_challenge")
    op.drop_table("message_log")
    op.drop_table("chat_member")
    op.drop_table("chat")
