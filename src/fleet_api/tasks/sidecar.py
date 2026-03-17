"""Sidecar (executor-side) event processing.

Handles events posted by the executor agent via the sidecar.
This module is separated from crud.py because it serves a different
auth model: executor-side operations vs principal-side (caller) operations.

See lifecycle.py for principal-side state transitions (cancel, pause, resume, etc.).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.errors import (
    AuthError,
    ErrorCode,
    InputValidationError,
    NotFoundError,
    StateError,
)
from fleet_api.tasks.callbacks import schedule_callback
from fleet_api.tasks.models import Task, TaskEvent, TaskStatus
from fleet_api.tasks.state_machine import InvalidStateTransition, is_terminal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid event types from executor sidecar
# ---------------------------------------------------------------------------

_VALID_EVENT_TYPES = frozenset(
    {
        "status",
        "progress",
        "log",
        "completed",
        "failed",
        "heartbeat",
        "context_injected",
        "escalation",
    }
)


async def process_sidecar_event(
    session: AsyncSession,
    task_id: str,
    event_type: str,
    data: dict[str, Any],
    sequence: int,
    executor_agent_id: str,
) -> tuple[TaskEvent, Task]:
    """Process an execution event from the sidecar.

    Args:
        session: Database session.
        task_id: Task to post the event against.
        event_type: One of status/progress/log/completed/failed/heartbeat.
        data: Event payload.
        sequence: Monotonically increasing sequence number.
        executor_agent_id: Authenticated agent posting the event.

    Returns:
        (event, task) tuple — the created TaskEvent and the updated Task.

    Raises:
        NotFoundError: Task not found.
        AuthError: Agent is not the task executor.
        InputValidationError: Invalid event_type or out-of-order sequence.
        StateError: Invalid status transition.
    """
    # 1. Validate event_type
    if event_type not in _VALID_EVENT_TYPES:
        valid = ", ".join(sorted(_VALID_EVENT_TYPES))
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message=f"Invalid event_type '{event_type}'.",
            suggestion=f"Use one of: {valid}.",
        )

    # 2. Fetch task
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found.",
            suggestion="Check the task ID.",
        )

    # 3. Authorization: only the executor can post events
    if task.executor_agent_id != executor_agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message=(
                f"Agent '{executor_agent_id}' is not the executor of task '{task_id}'. "
                f"Only the assigned executor can post events."
            ),
            suggestion="Authenticate as the task's executor agent.",
        )

    # 4. Sequence validation: must be strictly greater than the last sequence
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task_id)
    )
    last_sequence = max_seq_result.scalar_one()
    if sequence <= last_sequence:
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message=(
                f"Sequence {sequence} is not greater than the last sequence "
                f"{last_sequence} for task '{task_id}'."
            ),
            suggestion="Use a sequence number greater than the previous event.",
        )

    # 5. Process event by type
    now = datetime.now(UTC)

    if event_type == "status":
        # Phase 1: executor trusted for all status transitions. Phase 2: restrict to running/paused.
        new_status_str = data.get("status", "")
        try:
            new_status = TaskStatus(new_status_str)
        except ValueError:
            valid_statuses = ", ".join(s.value for s in TaskStatus)
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid status '{new_status_str}'.",
                suggestion=f"Use one of: {valid_statuses}.",
            )
        try:
            task.transition_to(new_status)
        except InvalidStateTransition:
            raise StateError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message=(
                    f"Cannot transition task '{task_id}' from "
                    f"'{task.status.value}' to '{new_status_str}'."
                ),
            )
        if new_status == TaskStatus.RUNNING and task.started_at is None:
            task.started_at = now

    elif event_type == "completed":
        try:
            task.transition_to(TaskStatus.COMPLETED)
        except InvalidStateTransition:
            raise StateError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message=(
                    f"Cannot transition task '{task_id}' from '{task.status.value}' to 'completed'."
                ),
            )
        task.result = data.get("result")
        if data.get("quality") is not None or data.get("warnings") is not None:
            task.metadata_ = task.metadata_ or {}
            if data.get("quality") is not None:
                task.metadata_["quality"] = data["quality"]
            if data.get("warnings") is not None:
                task.metadata_["warnings"] = data["warnings"]

    elif event_type == "failed":
        try:
            task.transition_to(TaskStatus.FAILED)
        except InvalidStateTransition:
            raise StateError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message=(
                    f"Cannot transition task '{task_id}' from '{task.status.value}' to 'failed'."
                ),
            )
        if "error_code" not in data or "message" not in data:
            logger.warning(
                "Failed event for task '%s' missing fields: error_code=%s, message=%s",
                task_id,
                "present" if "error_code" in data else "MISSING",
                "present" if "message" in data else "MISSING",
            )
        task.result = {"error_code": data.get("error_code"), "message": data.get("message")}

    elif event_type == "progress":
        if "progress" not in data:
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message="Progress event requires a 'progress' field in data.",
                suggestion="Include 'progress' (0-100) in the event data payload.",
            )
        if task.metadata_ is None:
            task.metadata_ = {}
        task.metadata_["progress"] = data["progress"]
        if data.get("message"):
            task.metadata_["progress_message"] = data["message"]

    # log and heartbeat: no task state changes, just record the event

    # 6. Create the event record
    event = TaskEvent(
        task_id=task_id,
        event_type=event_type,
        data=data,
        sequence=sequence,
        created_at=now,
    )
    session.add(event)

    # 7. Commit
    await session.commit()
    await session.refresh(task)
    await session.refresh(event)

    # 8. Schedule callback delivery for terminal events
    if is_terminal(task.status) and task.callback_url is not None:
        schedule_callback(task)

    return event, task
