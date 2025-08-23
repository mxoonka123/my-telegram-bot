"""
add proactive_messaging_rate to persona_configs

Revision ID: 20250823_120000
Revises: 20250820_200500
Create Date: 2025-08-23 12:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "20250823_120000"
down_revision = "20250820_200500"
branch_labels = None
depends_on = None


def _has_column(inspector: Inspector, table: str, column: str) -> bool:
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    # persona_configs.proactive_messaging_rate
    if not _has_column(inspector, "persona_configs", "proactive_messaging_rate"):
        op.add_column(
            "persona_configs",
            sa.Column("proactive_messaging_rate", sa.Text(), nullable=False, server_default="sometimes"),
        )
        # Убираем server_default после бэкфилла, чтобы не прилипал навсегда
        op.alter_column("persona_configs", "proactive_messaging_rate", server_default=None)


def downgrade() -> None:
    bind: Connection = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "persona_configs", "proactive_messaging_rate"):
        op.drop_column("persona_configs", "proactive_messaging_rate")
