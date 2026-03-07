"""Add paused_at column to tasks table.

Revision ID: 004_add_paused_at
Revises: 003_add_callback_url
Create Date: 2026-03-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_add_paused_at"
down_revision: str = "003_add_callback_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add paused_at column to tasks table (nullable, timezone-aware)."""
    op.add_column(
        "tasks",
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove paused_at column from tasks table."""
    op.drop_column("tasks", "paused_at")
