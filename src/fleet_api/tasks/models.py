"""Task and TaskEvent SQLAlchemy models."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fleet_api.database.base import Base
from fleet_api.tasks.state_machine import (
    InvalidStateTransition,
    TaskStatus,
    is_terminal,
    now_utc,
    validate_transition,
)


class TaskPriority(enum.Enum):
    """Task priority levels.

    RFC section 3.4 defines low, normal, high.  ``CRITICAL`` is an extension
    beyond the RFC spec, added for fleet-internal escalation semantics.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"  # Extension: not in RFC §3.4 (defines low/normal/high)


class Task(Base):
    """A task dispatched through a workflow."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("workflows.id"), nullable=False
    )
    principal_agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agents.id"), nullable=False
    )
    executor_agent_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("agents.id"), nullable=True
    )
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        server_default="accepted",
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    priority: Mapped[TaskPriority] = mapped_column(
        Enum(
            TaskPriority, name="task_priority", values_callable=lambda e: [x.value for x in e]
        ),
        nullable=False,
        server_default="normal",
    )
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("tasks.id"), nullable=True, index=True
    )
    root_task_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("tasks.id"), nullable=True, index=True
    )
    lineage_depth: Mapped[int] = mapped_column(
        "lineage_depth", Integer, server_default="0", nullable=False
    )
    delegation_depth: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    callback_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(256), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    def transition_to(self, new_status: TaskStatus) -> None:
        """Transition the task to a new status.

        Validates the transition against the state machine rules.
        If the new status is terminal, sets completed_at.

        Note:
            Callers are responsible for setting ``started_at`` when
            transitioning to RUNNING.  This method only manages
            ``completed_at`` for terminal states.

        Raises:
            InvalidStateTransition: If the transition is not allowed.
        """
        validate_transition(self.status, new_status)
        self.status = new_status
        if is_terminal(new_status):
            self.completed_at = now_utc()


class TaskEvent(Base):
    """An immutable event in a task's lifecycle.

    Note: In a future phase, this table may be partitioned by created_at
    for better query performance at scale.
    """

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("tasks.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Re-export for convenience — callers can import from tasks.models
__all__ = [
    "InvalidStateTransition",
    "Task",
    "TaskEvent",
    "TaskPriority",
    "TaskStatus",
]
