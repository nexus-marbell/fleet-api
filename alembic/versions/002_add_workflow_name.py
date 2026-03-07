"""Add name column to workflows table.

Revision ID: 002_add_workflow_name
Revises: 001_initial
Create Date: 2026-03-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_add_workflow_name"
down_revision: str = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add name column to workflows table (required, non-nullable)."""
    op.add_column(
        "workflows",
        sa.Column("name", sa.String(256), nullable=False, server_default="Unnamed"),
    )
    # Remove the server_default after backfilling so the app enforces it
    op.alter_column("workflows", "name", server_default=None)


def downgrade() -> None:
    """Remove name column from workflows table."""
    op.drop_column("workflows", "name")
