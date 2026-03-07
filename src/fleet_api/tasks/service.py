"""Task business logic — creation, dispatch, read, list, cancel, and sidecar operations."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.agents.models import Agent, AgentStatus
from fleet_api.config import settings
from fleet_api.errors import (
    AuthError,
    ErrorCode,
    InfrastructureError,
    InputValidationError,
    NotFoundError,
    StateError,
)
from fleet_api.tasks.callbacks import schedule_callback
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus
from fleet_api.tasks.state_machine import InvalidStateTransition, is_terminal
from fleet_api.workflows.models import Workflow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDEMPOTENCY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Status -> links reference table (RFC section 3.6)
# ---------------------------------------------------------------------------
#
# Single source of truth for which action links appear per task status.
# Every status also gets: self, workflow, stream (unconditionally).
# Action links use {"method": "POST", "href": "..."} per RFC.
#
# Reference:
#   accepted   -> cancel
#   running    -> cancel, pause, context, redirect
#   paused     -> resume, cancel, context, redirect
#   completed  -> retask, rerun
#   failed     -> retask, rerun
#   cancelled  -> rerun
#   retasked   -> (none)
#   redirected -> (none)

_STATUS_ACTION_LINKS: dict[TaskStatus, tuple[str, ...]] = {
    TaskStatus.ACCEPTED: ("cancel",),
    TaskStatus.RUNNING: ("cancel", "pause", "context", "redirect"),
    TaskStatus.PAUSED: ("resume", "cancel", "context", "redirect"),
    TaskStatus.COMPLETED: ("retask", "rerun"),
    TaskStatus.FAILED: ("retask", "rerun"),
    TaskStatus.CANCELLED: ("rerun",),
    TaskStatus.RETASKED: (),
    TaskStatus.REDIRECTED: (),
}

# Path suffix for each action link (relative to task base path).
# "rerun" is special — it points to /workflows/{wf}/run, handled separately.
_ACTION_LINK_SUFFIX: dict[str, str] = {
    "cancel": "/cancel",
    "pause": "/pause",
    "resume": "/resume",
    "context": "/context",
    "redirect": "/redirect",
    "retask": "/retask",
    # "rerun" handled in build_task_links (different base path)
}


# ---------------------------------------------------------------------------
# HATEOAS link builder (shared across task endpoints)
# ---------------------------------------------------------------------------


def build_task_links(task_id: str, workflow_id: str, status: TaskStatus | str) -> dict[str, Any]:
    """Build state-dependent HATEOAS links for a task (RFC section 3.6).

    Uses the _STATUS_ACTION_LINKS reference table as the single source of
    truth. Non-action links (self, workflow, stream) are always present.
    Action links include ``method: "POST"`` per RFC.
    """
    if not isinstance(status, TaskStatus):
        status = TaskStatus(status)

    base = f"/workflows/{workflow_id}/tasks/{task_id}"
    links: dict[str, Any] = {
        "self": {"href": base},
        "workflow": {"href": f"/workflows/{workflow_id}"},
        "stream": {"href": f"{base}/stream"},
    }

    for action in _STATUS_ACTION_LINKS.get(status, ()):
        if action == "rerun":
            links["rerun"] = {"method": "POST", "href": f"/workflows/{workflow_id}/run"}
        else:
            links[action] = {"method": "POST", "href": f"{base}{_ACTION_LINK_SUFFIX[action]}"}

    return links


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _compute_duration(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    """Compute duration in seconds between started_at and completed_at."""
    if started_at is None or completed_at is None:
        return None
    return int((completed_at - started_at).total_seconds())


def task_to_detail_response(task: Task) -> dict[str, Any]:
    """Convert a Task model to a full detail response dict (RFC section 3.6)."""
    status = task.status if isinstance(task.status, TaskStatus) else TaskStatus(task.status)

    response: dict[str, Any] = {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "status": status.value,
        "caller": task.principal_agent_id,
        "executor": task.executor_agent_id,
        "priority": (
            task.priority.value if hasattr(task.priority, "value") else str(task.priority)
        ),
        "input": task.input,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

    if task.started_at is not None:
        response["started_at"] = task.started_at.isoformat()

    if status == TaskStatus.RUNNING:
        response["progress"] = (
            task.metadata_.get("progress", 0) if task.metadata_ else 0
        )
        response["estimated_completion"] = (
            task.metadata_.get("estimated_completion") if task.metadata_ else None
        )
    elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        response["result"] = task.result
        response["warnings"] = (
            task.metadata_.get("warnings", []) if task.metadata_ else []
        )
        if status == TaskStatus.COMPLETED:
            # Intentional: quality defaults to all-true when metadata is absent.
            # This is the happy-path assumption — callers that need to signal
            # degraded quality must explicitly set quality flags in task metadata.
            response["quality"] = (
                task.metadata_.get(
                    "quality",
                    {"input_valid": True, "execution_clean": True, "result_complete": True},
                )
                if task.metadata_
                else {"input_valid": True, "execution_clean": True, "result_complete": True}
            )
        response["completed_at"] = (
            task.completed_at.isoformat() if task.completed_at else None
        )
        response["duration_seconds"] = _compute_duration(task.started_at, task.completed_at)

    elif status in (TaskStatus.CANCELLED, TaskStatus.REDIRECTED, TaskStatus.RETASKED):
        if task.completed_at is not None:
            response["completed_at"] = task.completed_at.isoformat()

    response["_links"] = build_task_links(task.id, task.workflow_id, status)
    return response


def task_to_summary_response(task: Task) -> dict[str, Any]:
    """Convert a Task model to a list summary response dict (RFC section 3.7)."""
    status = task.status if isinstance(task.status, TaskStatus) else TaskStatus(task.status)

    response: dict[str, Any] = {
        "task_id": task.id,
        "status": status.value,
        "caller": task.principal_agent_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

    if task.completed_at is not None:
        response["completed_at"] = task.completed_at.isoformat()

    if status == TaskStatus.COMPLETED:
        response["duration_seconds"] = _compute_duration(task.started_at, task.completed_at)

    base = f"/workflows/{task.workflow_id}/tasks/{task.id}"
    response["_links"] = {
        "self": {"href": base},
        "stream": {"href": f"{base}/stream"},
    }
    return response


# ---------------------------------------------------------------------------
# Cursor pagination helpers
# ---------------------------------------------------------------------------


def encode_task_cursor(task_id: str, created_at: datetime) -> str:
    """Encode a task ID and created_at into an opaque base64 cursor."""
    payload = {"id": task_id, "created_at": created_at.isoformat()}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def decode_task_cursor(cursor: str) -> tuple[str, datetime]:
    """Decode an opaque base64 cursor to extract task ID and created_at."""
    try:
        data = json.loads(base64.b64decode(cursor))
        task_id = str(data["id"])
        created_at = datetime.fromisoformat(str(data["created_at"]))
        return task_id, created_at
    except (ValueError, KeyError, TypeError) as e:
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message="Invalid pagination cursor.",
            suggestion="Use the cursor value returned from a previous list response.",
        ) from e


# ---------------------------------------------------------------------------
# Standalone cancel operation
# ---------------------------------------------------------------------------


async def cancel_task(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    cancelled_by: str,
    reason: str | None = None,
) -> Task:
    """Cancel a task in accepted, running, or paused state.

    Args:
        session: Database session.
        workflow_id: Workflow the task belongs to.
        task_id: Task to cancel.
        cancelled_by: Agent requesting cancellation.
        reason: Optional human-readable reason.

    Returns:
        The cancelled Task.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is in a terminal state and cannot be cancelled.
    """
    # 1. Fetch workflow
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message=f"Workflow '{workflow_id}' not found.",
            suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
            links={"list": {"href": "/workflows"}},
        )

    # 2. Fetch task and verify it belongs to this workflow
    task = await session.get(Task, task_id)
    if task is None or task.workflow_id != workflow_id:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
            suggestion="Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks.",
            links={"workflow": {"href": f"/workflows/{workflow_id}"}},
        )

    # 3. Authorization: caller must be task principal or workflow owner
    if cancelled_by != task.principal_agent_id and cancelled_by != workflow.owner_agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message="Only the task caller or workflow owner may cancel this task.",
            suggestion="Authenticate as the task's principal agent or the workflow owner.",
        )

    # 4. Attempt state transition
    old_status = task.status
    try:
        task.transition_to(TaskStatus.CANCELLED)
    except InvalidStateTransition:
        raise StateError(
            code=ErrorCode.INVALID_STATE_TRANSITION,
            message=(
                f"Task '{task_id}' cannot be cancelled. "
                f"Current status: '{task.status.value}'. "
                f"Only tasks with status 'accepted', 'running', or 'paused' can be cancelled."
            ),
        )

    # 5. Create TaskEvent
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
            TaskEvent.task_id == task.id
        )
    )
    next_sequence = max_seq_result.scalar_one() + 1

    event = TaskEvent(
        task_id=task.id,
        event_type="status",
        data={
            "from_status": old_status.value,
            "to_status": "cancelled",
            "reason": reason,
            "cancelled_by": cancelled_by,
        },
        sequence=next_sequence,
    )
    session.add(event)

    # 6. Commit
    await session.commit()
    await session.refresh(task)

    return task


# ---------------------------------------------------------------------------
# Retask operation
# ---------------------------------------------------------------------------


async def retask_task(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    caller_agent_id: str,
    refinement: dict[str, Any],
    priority: str | None = None,
) -> tuple[Task, Task]:
    """Retask a completed or failed task with refinement instructions.

    Creates a new task linked to the original via parent_task_id/root_task_id,
    transitions the original task to RETASKED, and records a status event.

    Args:
        session: Database session.
        workflow_id: Workflow the task belongs to.
        task_id: Original task to retask.
        caller_agent_id: Agent requesting the retask.
        refinement: Refinement instructions (message, additional_input, constraints).
        priority: Optional priority override for the new task.

    Returns:
        (new_task, original_task) tuple.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in completed/failed state.
        InputValidationError: Retask depth exceeded.
    """
    # 1. Fetch workflow
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message=f"Workflow '{workflow_id}' not found.",
            suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
            links={"list": {"href": "/workflows"}},
        )

    # 2. Fetch task and verify it belongs to this workflow
    task = await session.get(Task, task_id)
    if task is None or task.workflow_id != workflow_id:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
            suggestion="Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks.",
            links={"workflow": {"href": f"/workflows/{workflow_id}"}},
        )

    # 3. Authorization: caller must be task principal or workflow owner
    if caller_agent_id != task.principal_agent_id and caller_agent_id != workflow.owner_agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message="Only the task caller or workflow owner may retask this task.",
            suggestion="Authenticate as the task's principal agent or the workflow owner.",
        )

    # 4. Validate task state (must be completed or failed)
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        raise StateError(
            code=ErrorCode.RETASK_NOT_REVIEWABLE,
            message=(
                f"Task '{task_id}' cannot be retasked. "
                f"Current status: '{task.status.value}'. "
                f"Only tasks with status 'completed' or 'failed' can be retasked."
            ),
        )

    # 5. Check lineage depth limit
    if task.lineage_depth >= settings.fleet_lineage_max_depth:
        raise InputValidationError(
            code=ErrorCode.RETASK_DEPTH_EXCEEDED,
            message=(
                f"Lineage depth limit ({settings.fleet_lineage_max_depth}) exceeded. "
                f"Task '{task_id}' is already at depth {task.lineage_depth}."
            ),
            suggestion="The retask chain has reached its maximum depth. Start a new task instead.",
        )

    # 6. Transition original task to RETASKED
    #    Status is already validated as COMPLETED or FAILED (step 4), and
    #    both have RETASKED as a valid transition, so this cannot fail.
    old_status = task.status
    task.transition_to(TaskStatus.RETASKED)

    # 7. Determine lineage
    root_task_id = task.root_task_id or task.id
    new_lineage_depth = task.lineage_depth + 1

    # 8. Build merged input
    merged_input = dict(task.input) if task.input else {}
    additional_input = refinement.get("additional_input")
    if additional_input:
        merged_input.update(additional_input)

    # 9. Resolve priority
    if priority is not None:
        try:
            priority_enum = TaskPriority(priority)
        except ValueError:
            valid = ", ".join(p.value for p in TaskPriority)
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid priority '{priority}'. Must be one of: {valid}.",
                suggestion=f"Use one of: {valid}.",
            )
    else:
        priority_enum = (
            task.priority
            if isinstance(task.priority, TaskPriority)
            else TaskPriority(task.priority)
        )

    # 10. Create new task
    new_task_id = f"task-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)

    new_task = Task(
        id=new_task_id,
        workflow_id=workflow_id,
        principal_agent_id=task.principal_agent_id,
        executor_agent_id=task.executor_agent_id,
        status=TaskStatus.ACCEPTED,
        input=merged_input,
        priority=priority_enum,
        timeout_seconds=task.timeout_seconds,
        parent_task_id=task.id,
        root_task_id=root_task_id,
        lineage_depth=new_lineage_depth,
        delegation_depth=task.delegation_depth,
        created_at=now,
        metadata_={"refinement": refinement},
    )
    session.add(new_task)

    # 11. Create status event on original task
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
            TaskEvent.task_id == task.id
        )
    )
    next_sequence = max_seq_result.scalar_one() + 1

    event = TaskEvent(
        task_id=task.id,
        event_type="status",
        data={
            "from_status": old_status.value,
            "to_status": "retasked",
            "retask_id": new_task_id,
            "retasked_by": caller_agent_id,
            "refinement_message": refinement.get("message"),
        },
        sequence=next_sequence,
        created_at=now,
    )
    session.add(event)

    # 12. Create initial event on new task
    new_event = TaskEvent(
        task_id=new_task_id,
        event_type="created",
        sequence=1,
        data={"status": "accepted", "caller": task.principal_agent_id, "retask_of": task.id},
        created_at=now,
    )
    session.add(new_event)

    # 13. Commit
    await session.commit()
    await session.refresh(new_task)
    await session.refresh(task)

    return new_task, task


async def build_lineage_chain(
    session: AsyncSession,
    task: Task,
) -> list[str]:
    """Build the lineage chain from root to current task.

    Walks parent_task_id links from the given task up to the root,
    then reverses to get root-first order.
    """
    chain = [task.id]
    current_parent_id = task.parent_task_id

    # Walk up to root (limit iterations for safety)
    for _ in range(task.lineage_depth):
        if current_parent_id is None:
            break
        parent = await session.get(Task, current_parent_id)
        if parent is None:
            break
        chain.append(parent.id)
        current_parent_id = parent.parent_task_id

    chain.reverse()
    return chain


async def count_context_injections(
    session: AsyncSession,
    task_id: str,
) -> int:
    """Count context_injected events on a task."""
    result = await session.execute(
        select(func.count()).where(
            TaskEvent.task_id == task_id,
            TaskEvent.event_type == "context_injected",
        )
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Redirect operation
# ---------------------------------------------------------------------------


async def redirect_task(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    caller_agent_id: str,
    reason: str,
    new_input: dict[str, Any],
    inherit_progress: bool = False,
    priority: str | None = None,
) -> tuple[Task, Task]:
    """Redirect a running or paused task with new input.

    Transitions the original task to REDIRECTED and creates a new task with
    the provided new_input (replaces original input entirely).  Lineage is
    tracked using the same parent_task_id / root_task_id / lineage_depth
    columns used by retask.

    Args:
        session: Database session.
        workflow_id: Workflow the task belongs to.
        task_id: Original task to redirect.
        caller_agent_id: Agent requesting the redirect.
        reason: Why the redirect is needed.
        new_input: Input for the new task (replaces original input entirely).
        inherit_progress: Whether new task inherits progress metadata.
        priority: Optional priority override for the new task.

    Returns:
        (new_task, original_task) tuple.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in RUNNING or PAUSED state.
        InputValidationError: Lineage depth exceeded.
    """
    # 1. Fetch workflow
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message=f"Workflow '{workflow_id}' not found.",
            suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
            links={"list": {"href": "/workflows"}},
        )

    # 2. Fetch task and verify it belongs to this workflow
    task = await session.get(Task, task_id)
    if task is None or task.workflow_id != workflow_id:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
            suggestion="Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks.",
            links={"workflow": {"href": f"/workflows/{workflow_id}"}},
        )

    # 3. Authorization: caller must be task principal or workflow owner
    if caller_agent_id != task.principal_agent_id and caller_agent_id != workflow.owner_agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message="Only the task caller or workflow owner may redirect this task.",
            suggestion="Authenticate as the task's principal agent or the workflow owner.",
        )

    # 4. Validate task state (must be RUNNING or PAUSED)
    if task.status not in (TaskStatus.RUNNING, TaskStatus.PAUSED):
        raise StateError(
            code=ErrorCode.REDIRECT_NOT_POSSIBLE,
            message=(
                f"Task '{task_id}' cannot be redirected. "
                f"Current status: '{task.status.value}'. "
                f"Only tasks with status 'running' or 'paused' can be redirected."
            ),
        )

    # 5. Check lineage depth limit
    if task.lineage_depth >= settings.fleet_lineage_max_depth:
        raise InputValidationError(
            code=ErrorCode.RETASK_DEPTH_EXCEEDED,
            message=(
                f"Lineage depth limit ({settings.fleet_lineage_max_depth}) exceeded. "
                f"Task '{task_id}' is already at depth {task.lineage_depth}."
            ),
            suggestion="The lineage chain has reached its maximum depth. Start a new task instead.",
        )

    # 6. Transition original task to REDIRECTED
    old_status = task.status
    task.transition_to(TaskStatus.REDIRECTED)

    # 7. Determine lineage
    root_task_id = task.root_task_id or task.id
    new_lineage_depth = task.lineage_depth + 1

    # 8. Resolve priority
    if priority is not None:
        try:
            priority_enum = TaskPriority(priority)
        except ValueError:
            valid = ", ".join(p.value for p in TaskPriority)
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid priority '{priority}'. Must be one of: {valid}.",
                suggestion=f"Use one of: {valid}.",
            )
    else:
        priority_enum = (
            task.priority
            if isinstance(task.priority, TaskPriority)
            else TaskPriority(task.priority)
        )

    # 9. Build metadata for new task
    new_metadata: dict[str, Any] = {"redirect_reason": reason}
    if inherit_progress and task.metadata_:
        progress = task.metadata_.get("progress")
        if progress is not None:
            new_metadata["progress"] = progress
        progress_message = task.metadata_.get("progress_message")
        if progress_message is not None:
            new_metadata["progress_message"] = progress_message

    # 10. Create new task
    new_task_id = f"task-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)

    new_task = Task(
        id=new_task_id,
        workflow_id=workflow_id,
        principal_agent_id=task.principal_agent_id,
        executor_agent_id=task.executor_agent_id,
        status=TaskStatus.ACCEPTED,
        input=new_input,
        priority=priority_enum,
        timeout_seconds=task.timeout_seconds,
        parent_task_id=task.id,
        root_task_id=root_task_id,
        lineage_depth=new_lineage_depth,
        delegation_depth=task.delegation_depth,
        created_at=now,
        metadata_=new_metadata,
    )
    session.add(new_task)

    # 11. Create status event on original task ("redirected")
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
            TaskEvent.task_id == task.id
        )
    )
    next_sequence = max_seq_result.scalar_one() + 1

    event = TaskEvent(
        task_id=task.id,
        event_type="status",
        data={
            "from_status": old_status.value,
            "to_status": "redirected",
            "reason": reason,
            "redirect_id": new_task_id,
            "redirected_by": caller_agent_id,
        },
        sequence=next_sequence,
        created_at=now,
    )
    session.add(event)

    # 12. Create initial event on new task
    new_event = TaskEvent(
        task_id=new_task_id,
        event_type="created",
        sequence=1,
        data={
            "status": "accepted",
            "caller": task.principal_agent_id,
            "redirect_of": task.id,
        },
        created_at=now,
    )
    session.add(new_event)

    # 13. Commit
    await session.commit()
    await session.refresh(new_task)
    await session.refresh(task)

    return new_task, task


# ---------------------------------------------------------------------------
# Standalone pause operation
# ---------------------------------------------------------------------------


async def pause_task(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    paused_by: str,
    reason: str | None = None,
) -> tuple[Task, TaskEvent]:
    """Pause a running task.

    Args:
        session: Database session.
        workflow_id: Workflow the task belongs to.
        task_id: Task to pause.
        paused_by: Agent requesting the pause.
        reason: Optional human-readable reason.

    Returns:
        (task, event) tuple — the paused Task and the created TaskEvent.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in RUNNING state.
    """
    # 1. Fetch workflow
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message=f"Workflow '{workflow_id}' not found.",
            suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
            links={"list": {"href": "/workflows"}},
        )

    # 2. Fetch task and verify it belongs to this workflow
    task = await session.get(Task, task_id)
    if task is None or task.workflow_id != workflow_id:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
            suggestion="Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks.",
            links={"workflow": {"href": f"/workflows/{workflow_id}"}},
        )

    # 3. Authorization: caller must be task principal or workflow owner
    if paused_by != task.principal_agent_id and paused_by != workflow.owner_agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message="Only the task caller or workflow owner may pause this task.",
            suggestion="Authenticate as the task's principal agent or the workflow owner.",
        )

    # 4. Validate task is RUNNING (not just "can transition to PAUSED")
    if task.status != TaskStatus.RUNNING:
        raise StateError(
            code=ErrorCode.TASK_NOT_PAUSABLE,
            message=f"Task '{task_id}' cannot be paused. Current status: '{task.status.value}'.",
            suggestion="Only tasks with status 'running' can be paused.",
        )

    # 5. Transition to PAUSED
    old_status = task.status
    try:
        task.transition_to(TaskStatus.PAUSED)
    except InvalidStateTransition:
        raise StateError(
            code=ErrorCode.TASK_NOT_PAUSABLE,
            message=f"Task '{task_id}' cannot be paused. Current status: '{task.status.value}'.",
            suggestion="Only tasks with status 'running' can be paused.",
        )

    # 6. Record paused_at
    now = datetime.now(UTC)
    task.paused_at = now

    # 7. Store progress from metadata for the paused_state
    progress = task.metadata_.get("progress", 0) if task.metadata_ else 0
    progress_message = task.metadata_.get("progress_message") if task.metadata_ else None

    # 8. Build paused_state
    pause_ttl = settings.fleet_pause_ttl_seconds
    expires_at = now + timedelta(seconds=pause_ttl)
    paused_state = {
        "progress": progress,
        "message": progress_message,
        "resumable": True,
        "state_ttl_seconds": pause_ttl,
        "expires_at": expires_at.isoformat(),
    }

    # 9. Create TaskEvent (pause_requested — informational for sidecar)
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
            TaskEvent.task_id == task.id
        )
    )
    next_sequence = max_seq_result.scalar_one() + 1

    event = TaskEvent(
        task_id=task.id,
        event_type="pause_requested",
        data={
            "from_status": old_status.value,
            "to_status": "paused",
            "reason": reason,
            "paused_by": paused_by,
            "paused_state": paused_state,
        },
        sequence=next_sequence,
    )
    session.add(event)

    # 10. Commit
    await session.commit()
    await session.refresh(task)
    await session.refresh(event)

    return task, event


# ---------------------------------------------------------------------------
# Standalone resume operation
# ---------------------------------------------------------------------------


async def resume_task(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    resumed_by: str,
    priority: str | None = None,
) -> tuple[Task, TaskEvent]:
    """Resume a paused task.

    Args:
        session: Database session.
        workflow_id: Workflow the task belongs to.
        task_id: Task to resume.
        resumed_by: Agent requesting the resume.
        priority: Optional priority override.

    Returns:
        (task, event) tuple — the resumed Task and the created TaskEvent.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in PAUSED state, or pause TTL has expired.
    """
    # 1. Fetch workflow
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message=f"Workflow '{workflow_id}' not found.",
            suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
            links={"list": {"href": "/workflows"}},
        )

    # 2. Fetch task and verify it belongs to this workflow
    task = await session.get(Task, task_id)
    if task is None or task.workflow_id != workflow_id:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
            suggestion="Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks.",
            links={"workflow": {"href": f"/workflows/{workflow_id}"}},
        )

    # 3. Authorization: caller must be task principal or workflow owner
    if resumed_by != task.principal_agent_id and resumed_by != workflow.owner_agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message="Only the task caller or workflow owner may resume this task.",
            suggestion="Authenticate as the task's principal agent or the workflow owner.",
        )

    # 4. Validate task is PAUSED
    if task.status != TaskStatus.PAUSED:
        raise StateError(
            code=ErrorCode.TASK_NOT_PAUSED,
            message=f"Task '{task_id}' cannot be resumed. Current status: '{task.status.value}'.",
            suggestion="Only tasks with status 'paused' can be resumed.",
        )

    # 5. Check TTL hasn't expired
    now = datetime.now(UTC)
    if task.paused_at is not None:
        elapsed = (now - task.paused_at).total_seconds()
        if elapsed > settings.fleet_pause_ttl_seconds:
            # Auto-cancel the task due to pause timeout
            try:
                task.transition_to(TaskStatus.CANCELLED)
            except InvalidStateTransition:
                pass  # Should not happen — PAUSED->CANCELLED is valid

            # Create timeout event
            max_seq_result = await session.execute(
                select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
                    TaskEvent.task_id == task.id
                )
            )
            next_sequence = max_seq_result.scalar_one() + 1

            timeout_event = TaskEvent(
                task_id=task.id,
                event_type="status",
                data={
                    "from_status": "paused",
                    "to_status": "cancelled",
                    "reason": "PAUSE_TIMEOUT",
                    "paused_duration_seconds": int(elapsed),
                },
                sequence=next_sequence,
            )
            session.add(timeout_event)
            await session.commit()
            await session.refresh(task)

            raise StateError(
                code=ErrorCode.PAUSE_TIMEOUT,
                message=(
                    f"Pause TTL expired for task '{task_id}'. "
                    f"Task was paused for {int(elapsed)} seconds "
                    f"(TTL: {settings.fleet_pause_ttl_seconds}s). "
                    f"Task has been auto-cancelled."
                ),
                suggestion="Create a new task. The paused state has expired.",
            )

    # 6. Transition to RUNNING
    old_status = task.status
    try:
        task.transition_to(TaskStatus.RUNNING)
    except InvalidStateTransition:
        raise StateError(
            code=ErrorCode.TASK_NOT_PAUSED,
            message=f"Task '{task_id}' cannot be resumed. Current status: '{task.status.value}'.",
            suggestion="Only tasks with status 'paused' can be resumed.",
        )

    # 7. Calculate paused duration
    paused_duration_seconds = (
        int((now - task.paused_at).total_seconds()) if task.paused_at else 0
    )

    # 8. Clear paused_at (task is no longer paused)
    task.paused_at = None

    # 9. Optionally update priority
    if priority is not None:
        try:
            priority_enum = TaskPriority(priority)
            task.priority = priority_enum
        except ValueError:
            valid = ", ".join(p.value for p in TaskPriority)
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid priority '{priority}'. Must be one of: {valid}.",
                suggestion=f"Use one of: {valid}.",
            )

    # 10. Create TaskEvent (resume_requested — informational for sidecar)
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
            TaskEvent.task_id == task.id
        )
    )
    next_sequence = max_seq_result.scalar_one() + 1

    event = TaskEvent(
        task_id=task.id,
        event_type="resume_requested",
        data={
            "from_status": old_status.value,
            "to_status": "running",
            "resumed_by": resumed_by,
            "paused_duration_seconds": paused_duration_seconds,
            "priority": (
                task.priority.value
                if isinstance(task.priority, TaskPriority)
                else str(task.priority)
            ),
        },
        sequence=next_sequence,
    )
    session.add(event)

    # 11. Commit
    await session.commit()
    await session.refresh(task)
    await session.refresh(event)

    return task, event


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TaskService:
    """Business logic for task creation, dispatch, read, and list operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_workflow_or_404(self, workflow_id: str) -> Workflow:
        """Fetch a workflow by ID. Raises NotFoundError if not found."""
        workflow = await self.session.get(Workflow, workflow_id)
        if workflow is None:
            raise NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message=f"Workflow '{workflow_id}' not found.",
                suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
                links={"workflows": "/workflows"},
            )
        return workflow

    async def _verify_workflow_exists(self, workflow_id: str) -> None:
        """Raise NotFoundError if the workflow does not exist."""
        await self.get_workflow_or_404(workflow_id)

    def validate_input(
        self, input_data: dict[str, Any], input_schema: dict[str, Any] | None
    ) -> None:
        """Validate input_data against a JSON Schema. Skip if no schema defined."""
        if input_schema is None:
            return
        try:
            jsonschema.validate(instance=input_data, schema=input_schema)
        except jsonschema.ValidationError as exc:
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Input validation failed: {exc.message}",
                suggestion="Check the input against the workflow's input_schema.",
                links={"workflow_schema": "/workflows"},
            ) from exc

    async def _check_agent_not_suspended(self, agent_id: str) -> None:
        """Check that the effective executor agent is not suspended."""
        agent = await self.session.get(Agent, agent_id)
        if agent is not None and agent.status == AgentStatus.SUSPENDED:
            raise InfrastructureError(
                code=ErrorCode.AGENT_SUSPENDED,
                message=f"Executor agent '{agent_id}' is suspended.",
                suggestion="The executor agent is currently unavailable. Try again later.",
            )

    async def _check_idempotency(
        self,
        idempotency_key: str,
        input_data: dict[str, Any],
    ) -> Task | None:
        """Check for an existing task with the same idempotency key.

        Returns the existing Task if found with matching input (replay).
        Raises IDEMPOTENCY_MISMATCH if found with different input.
        Returns None if no existing task found (new creation).
        """
        stmt = select(Task).where(Task.idempotency_key == idempotency_key)
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is None:
            return None

        existing_hash = hashlib.sha256(
            json.dumps(existing.input, sort_keys=True).encode()
        ).hexdigest()
        new_hash = hashlib.sha256(
            json.dumps(input_data, sort_keys=True).encode()
        ).hexdigest()

        if existing_hash == new_hash:
            return existing

        raise InputValidationError(
            code=ErrorCode.IDEMPOTENCY_MISMATCH,
            message=(
                f"Idempotency key '{idempotency_key}' was already used with different input."
            ),
            suggestion=(
                "Use a new idempotency key for different input, "
                "or resend the original input."
            ),
        )

    async def create_task(
        self,
        workflow_id: str,
        caller_agent_id: str,
        input_data: dict[str, Any],
        executor_agent_id: str | None = None,
        priority: str = "normal",
        timeout_seconds: int | None = None,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        callback_url: str | None = None,
    ) -> tuple[Task, Workflow, bool]:
        """Create a task for a workflow.

        Returns (task, workflow, is_replay). is_replay is True if an existing
        task was returned via idempotency replay.
        """
        if idempotency_key is not None:
            existing = await self._check_idempotency(idempotency_key, input_data)
            if existing is not None:
                workflow = await self.get_workflow_or_404(workflow_id)
                return existing, workflow, True

        workflow = await self.get_workflow_or_404(workflow_id)
        effective_executor = executor_agent_id or workflow.owner_agent_id
        await self._check_agent_not_suspended(effective_executor)
        self.validate_input(input_data, workflow.input_schema)

        try:
            priority_enum = TaskPriority(priority)
        except ValueError:
            valid = ", ".join(p.value for p in TaskPriority)
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid priority '{priority}'. Must be one of: {valid}.",
                suggestion=f"Use one of: {valid}.",
            )

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        effective_timeout = timeout_seconds or workflow.timeout_seconds
        now = datetime.now(UTC)

        task = Task(
            id=task_id,
            workflow_id=workflow_id,
            principal_agent_id=caller_agent_id,
            executor_agent_id=effective_executor,
            status=TaskStatus.ACCEPTED,
            input=input_data,
            priority=priority_enum,
            timeout_seconds=effective_timeout,
            callback_url=callback_url,
            idempotency_key=idempotency_key,
            created_at=now,
            metadata_=metadata,
        )
        self.session.add(task)

        event = TaskEvent(
            task_id=task_id,
            event_type="created",
            sequence=1,
            data={"status": "accepted", "caller": caller_agent_id},
            created_at=now,
        )
        self.session.add(event)

        await self.session.commit()
        await self.session.refresh(task)
        return task, workflow, False

    def build_task_response(
        self,
        task: Task,
        workflow: Workflow,
        is_replay: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Build the RFC-compliant task response dict for dispatch (POST /run)."""
        status_value = (
            task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
        )

        response: dict[str, Any] = {
            "task_id": task.id,
            "workflow_id": task.workflow_id,
            "status": status_value,
            "caller": task.principal_agent_id,
            "executor": task.executor_agent_id,
            "input": task.input,
            "priority": (
                task.priority.value
                if isinstance(task.priority, TaskPriority)
                else str(task.priority)
            ),
            "timeout_seconds": task.timeout_seconds,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "estimated_duration_seconds": workflow.estimated_duration_seconds,
            "_links": build_task_links(task.id, task.workflow_id, status_value),
        }

        effective_key = idempotency_key or task.idempotency_key
        if effective_key is not None:
            expires_at = (
                task.created_at + timedelta(hours=IDEMPOTENCY_TTL_HOURS)
                if task.created_at
                else None
            )
            response["idempotency"] = {
                "key": effective_key,
                "status": "replayed" if is_replay else "created",
                "expires_at": expires_at.isoformat() if expires_at else None,
            }

        return response

    async def get_task(self, workflow_id: str, task_id: str) -> Task:
        """Get a single task by ID within a workflow."""
        await self._verify_workflow_exists(workflow_id)

        task = await self.session.get(Task, task_id)
        if task is None or task.workflow_id != workflow_id:
            raise NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
                suggestion=(
                    "Check the task ID. "
                    "Use GET /workflows/{workflow_id}/tasks to list tasks."
                ),
                links={"tasks": {"href": f"/workflows/{workflow_id}/tasks"}},
            )
        return task

    async def list_tasks(
        self,
        workflow_id: str,
        status: str | None = None,
        priority: str | None = None,
        caller: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[Task], str | None, bool, int]:
        """List tasks for a workflow with filtering and cursor pagination.

        Returns (tasks, next_cursor, has_more, total_count).
        """
        await self._verify_workflow_exists(workflow_id)

        base_stmt = (
            select(Task)
            .where(Task.workflow_id == workflow_id)
            .order_by(Task.created_at.desc(), Task.id.desc())
        )

        if status is not None:
            try:
                status_enum = TaskStatus(status)
            except ValueError:
                valid_values = ", ".join(s.value for s in TaskStatus)
                raise InputValidationError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid status filter '{status}'.",
                    suggestion=f"Use one of: {valid_values}.",
                )
            base_stmt = base_stmt.where(Task.status == status_enum)

        if priority is not None:
            try:
                priority_enum = TaskPriority(priority)
            except ValueError:
                valid_values = ", ".join(p.value for p in TaskPriority)
                raise InputValidationError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid priority filter '{priority}'.",
                    suggestion=f"Use one of: {valid_values}.",
                )
            base_stmt = base_stmt.where(Task.priority == priority_enum)

        if caller is not None:
            base_stmt = base_stmt.where(Task.principal_agent_id == caller)

        if since is not None:
            try:
                since_dt = datetime.fromisoformat(since)
            except ValueError:
                raise InputValidationError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid 'since' timestamp: '{since}'.",
                    suggestion="Use ISO 8601 format, e.g. '2026-03-07T00:00:00Z'.",
                )
            base_stmt = base_stmt.where(Task.created_at >= since_dt)

        if until is not None:
            try:
                until_dt = datetime.fromisoformat(until)
            except ValueError:
                raise InputValidationError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid 'until' timestamp: '{until}'.",
                    suggestion="Use ISO 8601 format, e.g. '2026-03-07T23:59:59Z'.",
                )
            base_stmt = base_stmt.where(Task.created_at <= until_dt)

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total_count = (await self.session.execute(count_stmt)).scalar_one()

        # Apply cursor (created_at DESC ordering — cursor means "older than").
        # Use (created_at, task_id) as tiebreaker for tasks with identical
        # created_at timestamps.
        stmt = base_stmt
        if cursor is not None:
            cursor_task_id, cursor_created_at = decode_task_cursor(cursor)
            stmt = stmt.where(
                tuple_(Task.created_at, Task.id) < tuple_(
                    literal(cursor_created_at), literal(cursor_task_id)
                )
            )

        stmt = stmt.limit(limit + 1)
        result = await self.session.execute(stmt)
        tasks = list(result.scalars().all())

        has_more = len(tasks) > limit
        if has_more:
            tasks = tasks[:limit]

        next_cursor: str | None = None
        if has_more and tasks:
            last_task = tasks[-1]
            next_cursor = encode_task_cursor(last_task.id, last_task.created_at)

        return tasks, next_cursor, has_more, total_count

    async def get_pending_tasks(self, agent_id: str) -> list[Task]:
        """Return tasks assigned to *agent_id* in ``accepted`` status.

        Ordered by priority DESC (critical > high > normal > low) then
        created_at ASC (oldest first within same priority).
        """
        # Priority ordering: map enum values to sort weight (higher = first)
        priority_order = func.case(
            (Task.priority == TaskPriority.CRITICAL, 4),
            (Task.priority == TaskPriority.HIGH, 3),
            (Task.priority == TaskPriority.NORMAL, 2),
            (Task.priority == TaskPriority.LOW, 1),
            else_=0,
        )

        stmt = (
            select(Task)
            .where(Task.executor_agent_id == agent_id)
            .where(Task.status == TaskStatus.ACCEPTED)
            .order_by(priority_order.desc(), Task.created_at.asc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Sidecar event processing
# ---------------------------------------------------------------------------

# Valid sidecar event types.
_VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {"status", "progress", "log", "completed", "failed", "heartbeat",
     "context_injected", "escalation"}
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
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(
            TaskEvent.task_id == task_id
        )
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
                    f"Cannot transition task '{task_id}' from "
                    f"'{task.status.value}' to 'completed'."
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
                    f"Cannot transition task '{task_id}' from "
                    f"'{task.status.value}' to 'failed'."
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
