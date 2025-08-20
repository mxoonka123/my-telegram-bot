"""
add acl fields and credits

Revision ID: 20250820_200500
Revises: 20250820_190600
Create Date: 2025-08-20 20:05:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "20250820_200500"
down_revision = "20250820_190600"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def _has_index(inspector: Inspector, table: str, index_name: str) -> bool:
    return any(idx["name"] == index_name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    # users.credits
    if not _has_column(inspector, "users", "credits"):
        op.add_column("users", sa.Column("credits", sa.Float(), nullable=False, server_default="0"))
        # Drop server_default to avoid it sticking for future inserts if not desired
        op.alter_column("users", "credits", server_default=None)

    # bot_instances.access_level
    if not _has_column(inspector, "bot_instances", "access_level"):
        op.add_column("bot_instances", sa.Column("access_level", sa.String(), nullable=False, server_default="owner_only"))
        op.alter_column("bot_instances", "access_level", server_default=None)

    # bot_instances.whitelisted_users_json
    if not _has_column(inspector, "bot_instances", "whitelisted_users_json"):
        op.add_column("bot_instances", sa.Column("whitelisted_users_json", sa.Text(), nullable=True))
        # set default empty list for existing rows (best-effort)
        op.execute("UPDATE bot_instances SET whitelisted_users_json = '[]' WHERE whitelisted_users_json IS NULL")

    # index on access_level
    if not _has_index(inspector, "bot_instances", "ix_bot_instances_access_level"):
        op.create_index("ix_bot_instances_access_level", "bot_instances", ["access_level"], unique=False)


def downgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    # drop index
    if _has_index(inspector, "bot_instances", "ix_bot_instances_access_level"):
        op.drop_index("ix_bot_instances_access_level", table_name="bot_instances")

    # drop columns (safe checks)
    if _has_column(inspector, "bot_instances", "whitelisted_users_json"):
        op.drop_column("bot_instances", "whitelisted_users_json")
    if _has_column(inspector, "bot_instances", "access_level"):
        op.drop_column("bot_instances", "access_level")
    if _has_column(inspector, "users", "credits"):
        op.drop_column("users", "credits")
