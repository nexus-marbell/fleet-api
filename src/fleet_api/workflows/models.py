"""Workflow SQLAlchemy models."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fleet_api.database.base import Base


class WorkflowStatus(enum.Enum):
    """Workflow lifecycle states."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"


class Workflow(Base):
    """A workflow definition owned by an agent."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agents.id"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    estimated_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_retention_days: Mapped[int] = mapped_column(Integer, server_default="30", nullable=False)
    status: Mapped[WorkflowStatus] = mapped_column(
        Enum(
            WorkflowStatus,
            name="workflow_status",
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
        server_default="active",
    )
    tags: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
