"""Rename retask_depth to lineage_depth.

The column is now used by both retask and redirect operations, so the name
``retask_depth`` is semantically misleading.  Rename to ``lineage_depth``
to accurately reflect that it tracks depth across all lineage operations.

Revision ID: 005_rename_retask_depth
Revises: 004_add_paused_at
Create Date: 2026-03-07
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_rename_retask_depth"
down_revision: str = "004_add_paused_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename retask_depth -> lineage_depth on tasks table."""
    op.alter_column("tasks", "retask_depth", new_column_name="lineage_depth")


def downgrade() -> None:
    """Revert lineage_depth -> retask_depth on tasks table."""
    op.alter_column("tasks", "lineage_depth", new_column_name="retask_depth")
