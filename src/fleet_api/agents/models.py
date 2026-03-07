"""Agent SQLAlchemy models."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from fleet_api.database.base import Base


class AgentStatus(enum.Enum):
    """Agent lifecycle states."""

    REGISTERED = "registered"
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    SUSPENDED = "suspended"


class Agent(Base):
    """An agent registered with the fleet."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, name="agent_status", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        server_default="registered",
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
