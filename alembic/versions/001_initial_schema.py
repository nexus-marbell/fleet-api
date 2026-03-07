"""Initial schema: agents, workflows, tasks, task_events.

Revision ID: 001_initial
Revises:
Create Date: 2026-03-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create initial tables."""
    # --- Enum types ---
    agent_status = sa.Enum(
        "registered", "active", "unreachable", "suspended", name="agent_status"
    )
    workflow_status = sa.Enum("active", "deprecated", name="workflow_status")
    task_status = sa.Enum(
        "accepted",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "retasked",
        "redirected",
        name="task_status",
    )
    task_priority = sa.Enum("low", "normal", "high", "critical", name="task_priority")

    # --- agents ---
    op.create_table(
        "agents",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("public_key", sa.Text, nullable=False),
        sa.Column("capabilities", JSONB, nullable=True),
        sa.Column("status", agent_status, nullable=False, server_default="registered"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("metadata", JSONB, nullable=True),
    )

    # --- workflows ---
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "owner_agent_id",
            sa.String(128),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("input_schema", JSONB, nullable=True),
        sa.Column("output_schema", JSONB, nullable=True),
        sa.Column("estimated_duration_seconds", sa.Integer, nullable=True),
        sa.Column("timeout_seconds", sa.Integer, nullable=True),
        sa.Column(
            "result_retention_days", sa.Integer, server_default="30", nullable=False
        ),
        sa.Column(
            "status", workflow_status, nullable=False, server_default="active"
        ),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- tasks ---
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "workflow_id",
            sa.String(128),
            sa.ForeignKey("workflows.id"),
            nullable=False,
        ),
        sa.Column(
            "principal_agent_id",
            sa.String(128),
            sa.ForeignKey("agents.id"),
            nullable=False,
        ),
        sa.Column(
            "executor_agent_id",
            sa.String(128),
            sa.ForeignKey("agents.id"),
            nullable=True,
        ),
        sa.Column(
            "status", task_status, nullable=False, server_default="accepted"
        ),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column(
            "priority", task_priority, nullable=False, server_default="normal"
        ),
        sa.Column("timeout_seconds", sa.Integer, nullable=True),
        sa.Column(
            "parent_task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id"),
            nullable=True,
        ),
        sa.Column(
            "root_task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id"),
            nullable=True,
        ),
        sa.Column("retask_depth", sa.Integer, server_default="0", nullable=False),
        sa.Column(
            "delegation_depth", sa.Integer, server_default="0", nullable=False
        ),
        sa.Column("idempotency_key", sa.String(256), unique=True, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
    )

    # --- task_events ---
    # Note: In a future phase, this table may be partitioned by created_at
    # for better query performance at scale.
    op.create_table(
        "task_events",
        sa.Column(
            "id", sa.BigInteger, primary_key=True, autoincrement=True
        ),
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("data", JSONB, nullable=True),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- Indexes for common query patterns ---
    op.create_index("ix_tasks_workflow_id", "tasks", ["workflow_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_principal_agent_id", "tasks", ["principal_agent_id"])
    op.create_index("ix_tasks_executor_agent_id", "tasks", ["executor_agent_id"])
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index(
        "ix_task_events_task_id_sequence",
        "task_events",
        ["task_id", "sequence"],
        unique=True,
    )


def downgrade() -> None:
    """Drop all tables and enum types."""
    op.drop_index("ix_task_events_task_id_sequence", "task_events")
    op.drop_index("ix_task_events_task_id", "task_events")
    op.drop_index("ix_tasks_executor_agent_id", "tasks")
    op.drop_index("ix_tasks_principal_agent_id", "tasks")
    op.drop_index("ix_tasks_status", "tasks")
    op.drop_index("ix_tasks_workflow_id", "tasks")

    op.drop_table("task_events")
    op.drop_table("tasks")
    op.drop_table("workflows")
    op.drop_table("agents")

    # Drop enum types
    sa.Enum(name="task_priority").drop(op.get_bind())
    sa.Enum(name="task_status").drop(op.get_bind())
    sa.Enum(name="workflow_status").drop(op.get_bind())
    sa.Enum(name="agent_status").drop(op.get_bind())
