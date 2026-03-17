"""Task lifecycle operations — cancel, retask, redirect, pause, resume, lineage."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.config import settings
from fleet_api.errors import (
    AuthError,
    ErrorCode,
    InputValidationError,
    NotFoundError,
    StateError,
)
from fleet_api.tasks.crud import check_idempotency
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus
from fleet_api.tasks.state_machine import InvalidStateTransition
from fleet_api.workflows.models import Workflow

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
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task.id)
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
    idempotency_key: str | None = None,
) -> tuple[Task, Task, bool]:
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
        idempotency_key: Optional idempotency key for the new task.

    Returns:
        (new_task, original_task, is_replay) tuple.  is_replay is True when
        an existing task was returned via idempotency replay.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in completed/failed state.
        InputValidationError: Retask depth exceeded or idempotency mismatch.
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

    # 6. Build merged input (before state transition — needed for idempotency check)
    merged_input = dict(task.input) if task.input else {}
    additional_input = refinement.get("additional_input")
    if additional_input:
        merged_input.update(additional_input)

    # 6b. Idempotency check (before state transition — must not mutate task on replay)
    if idempotency_key is not None:
        existing = await check_idempotency(session, idempotency_key, merged_input)
        if existing is not None:
            return existing, task, True

    # 7. Transition original task to RETASKED
    #    Status is already validated as COMPLETED or FAILED (step 4), and
    #    both have RETASKED as a valid transition, so this cannot fail.
    old_status = task.status
    task.transition_to(TaskStatus.RETASKED)

    # 8. Determine lineage
    root_task_id = task.root_task_id or task.id
    new_lineage_depth = task.lineage_depth + 1

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
        idempotency_key=idempotency_key,
        metadata_={"refinement": refinement},
    )
    session.add(new_task)

    # 11. Create status event on original task
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task.id)
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

    return new_task, task, False


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
    idempotency_key: str | None = None,
) -> tuple[Task, Task, bool]:
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
        idempotency_key: Optional idempotency key for the new task.

    Returns:
        (new_task, original_task, is_replay) tuple.  is_replay is True when
        an existing task was returned via idempotency replay.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in RUNNING or PAUSED state.
        InputValidationError: Lineage depth exceeded or idempotency mismatch.
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

    # 5b. Idempotency check (before transitioning the original task)
    if idempotency_key is not None:
        existing = await check_idempotency(session, idempotency_key, new_input)
        if existing is not None:
            return existing, task, True

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
        idempotency_key=idempotency_key,
        metadata_=new_metadata,
    )
    session.add(new_task)

    # 11. Create status event on original task ("redirected")
    max_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task.id)
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

    return new_task, task, False


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
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task.id)
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
    paused_duration_seconds = int((now - task.paused_at).total_seconds()) if task.paused_at else 0

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
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task.id)
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
