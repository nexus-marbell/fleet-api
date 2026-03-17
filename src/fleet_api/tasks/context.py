"""Task context injection operations — count, sequence, inject."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.errors import (
    AuthError,
    ErrorCode,
    NotFoundError,
    StateError,
)
from fleet_api.tasks.models import Task, TaskEvent, TaskStatus
from fleet_api.workflows.models import Workflow


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


async def get_max_context_sequence(
    session: AsyncSession,
    task_id: str,
) -> int:
    """Return the highest context injection sequence for a task.

    Context injections use a SEPARATE sequence namespace from sidecar events.
    The sequence is derived from the ``sequence`` field stored inside the
    ``data`` JSON of ``context_injected`` events (as ``context_sequence``).
    Returns 0 if no context injections exist yet.
    """
    result = await session.execute(
        select(TaskEvent.data)
        .where(
            TaskEvent.task_id == task_id,
            TaskEvent.event_type == "context_injected",
        )
        .order_by(TaskEvent.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return 0
    return int(row.get("context_sequence", 0))


# ---------------------------------------------------------------------------
# Context injection operation
# ---------------------------------------------------------------------------

# Task statuses that accept context injection
_CONTEXT_INJECTABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.RUNNING, TaskStatus.PAUSED}
)


async def inject_context(
    session: AsyncSession,
    workflow_id: str,
    task_id: str,
    caller_agent_id: str,
    context_type: str,
    payload: dict[str, Any],
    sequence: int,
    urgency: str = "normal",
) -> dict[str, Any]:
    """Inject additional context into a running or paused task.

    Args:
        session: Database session.
        workflow_id: Workflow the task belongs to.
        task_id: Task to inject context into.
        caller_agent_id: Agent requesting the injection.
        context_type: One of: additional_input, constraint, correction, reference.
        payload: Context data with required ``message`` field.
        sequence: Monotonically increasing context sequence number.
        urgency: One of: low, normal, immediate. Default: normal.

    Returns:
        Response dict with context_id, task_id, context_type, sequence, status,
        accepted_at, and _links.

    Raises:
        NotFoundError: Workflow or task not found.
        AuthError: Caller is not the task principal or workflow owner.
        StateError: Task is not in RUNNING or PAUSED state, or sequence is
            out-of-order.
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
            message="Only the task caller or workflow owner may inject context.",
            suggestion="Authenticate as the task's principal agent or the workflow owner.",
        )

    # 4. State validation: only RUNNING or PAUSED tasks accept context injection
    if task.status not in _CONTEXT_INJECTABLE_STATUSES:
        injectable_names = ", ".join(sorted(s.value for s in _CONTEXT_INJECTABLE_STATUSES))
        raise StateError(
            code=ErrorCode.CONTEXT_REJECTED,
            message=(
                f"Context injection rejected for task '{task_id}'. "
                f"Current status: '{task.status.value}'. "
                f"Context can only be injected when task status is: {injectable_names}."
            ),
        )

    # 5. Sequence enforcement: must be strictly greater than last context sequence
    last_context_seq = await get_max_context_sequence(session, task_id)
    if sequence <= last_context_seq:
        raise StateError(
            code=ErrorCode.CONTEXT_REJECTED,
            message=(
                f"Out-of-sequence context injection for task '{task_id}'. "
                f"Received sequence {sequence}, but last accepted context "
                f"sequence is {last_context_seq}. "
                f"Sequence must be strictly greater than {last_context_seq}."
            ),
        )

    # 6. Create context_injected event (uses task_events sequence, not context sequence)
    max_event_seq_result = await session.execute(
        select(func.coalesce(func.max(TaskEvent.sequence), 0)).where(TaskEvent.task_id == task.id)
    )
    next_event_sequence = max_event_seq_result.scalar_one() + 1

    now = datetime.now(UTC)
    context_id = f"ctx-{uuid.uuid4().hex[:8]}"

    event = TaskEvent(
        task_id=task.id,
        event_type="context_injected",
        data={
            "context_id": context_id,
            "context_type": context_type,
            "context_sequence": sequence,
            "payload": payload,
            "urgency": urgency,
            "injected_by": caller_agent_id,
        },
        sequence=next_event_sequence,
        created_at=now,
    )
    session.add(event)

    # 7. Commit
    await session.commit()

    # 8. Build response (return caller-supplied context sequence, not global event sequence)
    return {
        "context_id": context_id,
        "task_id": task_id,
        "context_type": context_type,
        "sequence": sequence,
        "status": "accepted",
        "accepted_at": now.isoformat(),
        # TODO(Phase 2 Wave 3): Idempotency block deferred — see Issue #44
        "_links": {
            "task": {"href": f"/workflows/{workflow_id}/tasks/{task_id}"},
            "stream": {"href": f"/workflows/{workflow_id}/tasks/{task_id}/stream"},
            "workflow": {"href": f"/workflows/{workflow_id}"},
        },
    }
