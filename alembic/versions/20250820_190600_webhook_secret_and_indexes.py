"""
add webhook_secret to bot_instances and finalize schema (safe checks)

Revision ID: 20250820_190600
Revises: 
Create Date: 2025-08-20 19:06:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "20250820_190600"
down_revision = None
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table))


def _has_unique_constraint(inspector: Inspector, table: str, constraint_name: str) -> bool:
    for uc in inspector.get_unique_constraints(table):
        if uc.get("name") == constraint_name:
            return True
    # Некоторые БД отображают уникальные индексы как индексы
    for idx in inspector.get_indexes(table):
        if idx.get("name") == constraint_name and idx.get("unique"):
            return True
    return False


def upgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) Колонка webhook_secret
    if not _has_column(inspector, "bot_instances", "webhook_secret"):
        op.add_column("bot_instances", sa.Column("webhook_secret", sa.String(), nullable=True))

    # 2) Уникальная связь 1-1 по persona_config_id
    if not _has_unique_constraint(inspector, "bot_instances", "ux_bot_instances_persona_config_id"):
        try:
            op.create_unique_constraint(
                "ux_bot_instances_persona_config_id",
                "bot_instances",
                ["persona_config_id"],
            )
        except Exception:
            # fallback для БД, где уже есть уникальный индекс с другим именем
            pass

    # 3) Индексы для telegram_bot_id и telegram_username
    if not _has_index(inspector, "bot_instances", "ix_bot_instances_telegram_bot_id"):
        op.create_index(
            "ix_bot_instances_telegram_bot_id",
            "bot_instances",
            ["telegram_bot_id"],
            unique=False,
        )

    if not _has_index(inspector, "bot_instances", "ix_bot_instances_telegram_username"):
        op.create_index(
            "ix_bot_instances_telegram_username",
            "bot_instances",
            ["telegram_username"],
            unique=False,
        )


def downgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    # Откат индексов
    if _has_index(inspector, "bot_instances", "ix_bot_instances_telegram_username"):
        op.drop_index("ix_bot_instances_telegram_username", table_name="bot_instances")
    if _has_index(inspector, "bot_instances", "ix_bot_instances_telegram_bot_id"):
        op.drop_index("ix_bot_instances_telegram_bot_id", table_name="bot_instances")

    # Откат уникального ограничения
    if _has_unique_constraint(inspector, "bot_instances", "ux_bot_instances_persona_config_id"):
        try:
            op.drop_constraint(
                "ux_bot_instances_persona_config_id",
                "bot_instances",
                type_="unique",
            )
        except Exception:
            pass

    # Откат колонки webhook_secret
    if _has_column(inspector, "bot_instances", "webhook_secret"):
        op.drop_column("bot_instances", "webhook_secret")
