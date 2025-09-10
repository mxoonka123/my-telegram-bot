"""
create api_keys table for Gemini key rotation

Revision ID: 20250910_133600
Revises: 20250823_120000
Create Date: 2025-09-10 13:36:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "20250910_133600"
down_revision = "20250823_120000"
branch_labels = None
depends_on = None


def _has_table(inspector: Inspector, table: str) -> bool:
    return table in inspector.get_table_names()


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    try:
        return any(col["name"] == column for col in inspector.get_columns(table))
    except Exception:
        return False


def upgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    # Create table api_keys if not exists
    if not _has_table(inspector, "api_keys"):
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("service", sa.String(), nullable=False, server_default="gemini", index=True),
            sa.Column("api_key", sa.Text(), nullable=False, unique=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true"), index=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
            sa.Column("requests_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("comment", sa.String(), nullable=True),
        )
        # Remove server defaults where not needed long-term
        op.alter_column("api_keys", "service", server_default=None)
    else:
        # Ensure essential columns exist (idempotent safety)
        cols = {c["name"] for c in inspector.get_columns("api_keys")}
        if "is_active" not in cols:
            op.add_column("api_keys", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")))
            op.alter_column("api_keys", "is_active", server_default=None)
        if "service" not in cols:
            op.add_column("api_keys", sa.Column("service", sa.String(), nullable=False, server_default="gemini"))
            op.alter_column("api_keys", "service", server_default=None)
        if "api_key" not in cols:
            op.add_column("api_keys", sa.Column("api_key", sa.Text(), nullable=False, unique=True))
        if "last_used_at" not in cols:
            op.add_column("api_keys", sa.Column("last_used_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")))
        if "requests_count" not in cols:
            op.add_column("api_keys", sa.Column("requests_count", sa.Integer(), nullable=False, server_default="0"))
        if "comment" not in cols:
            op.add_column("api_keys", sa.Column("comment", sa.String(), nullable=True))

    # Ensure helpful indexes
    try:
        op.create_index("idx_api_keys_service", "api_keys", ["service"], if_not_exists=True)
    except TypeError:
        # Alembic <1.11 doesn't support if_not_exists; attempt create and ignore if exists
        try:
            op.create_index("idx_api_keys_service", "api_keys", ["service"]) 
        except Exception:
            pass
    try:
        op.create_index("idx_api_keys_active", "api_keys", ["is_active"], if_not_exists=True)
    except TypeError:
        try:
            op.create_index("idx_api_keys_active", "api_keys", ["is_active"]) 
        except Exception:
            pass


def downgrade() -> None:
    # Drop indexes first (best-effort)
    try:
        op.drop_index("idx_api_keys_active", table_name="api_keys")
    except Exception:
        pass
    try:
        op.drop_index("idx_api_keys_service", table_name="api_keys")
    except Exception:
        pass

    # Drop table
    try:
        op.drop_table("api_keys")
    except Exception:
        pass
