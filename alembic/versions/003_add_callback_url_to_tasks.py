"""Add callback_url column to tasks table.

Revision ID: 003_add_callback_url
Revises: 002_add_workflow_name
Create Date: 2026-03-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_add_callback_url"
down_revision: str = "002_add_workflow_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add callback_url column to tasks table (nullable, max 2048 chars)."""
    op.add_column(
        "tasks",
        sa.Column("callback_url", sa.String(2048), nullable=True),
    )


def downgrade() -> None:
    """Remove callback_url column from tasks table."""
    op.drop_column("tasks", "callback_url")
