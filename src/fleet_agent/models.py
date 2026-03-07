"""Shared data models for the fleet agent sidecar."""

from __future__ import annotations

from pydantic import BaseModel


class PendingTask(BaseModel):
    """A task retrieved from fleet-api awaiting local execution."""

    task_id: str
    workflow_id: str
    input: dict  # type: ignore[type-arg]
    priority: str
    timeout_seconds: int | None = None
    created_at: str


class TaskEvent(BaseModel):
    """An event emitted during task execution, streamed back to fleet-api."""

    event_type: str  # status, progress, log, completed, failed, heartbeat
    data: dict | None = None  # type: ignore[type-arg]
    sequence: int
