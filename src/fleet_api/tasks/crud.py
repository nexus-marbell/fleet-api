"""Task CRUD operations — TaskService class and sidecar event processing."""

from __future__ import annotations

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
from fleet_api.tasks.responses import (
    IDEMPOTENCY_TTL_HOURS,
    build_task_links,
    decode_task_cursor,
    encode_task_cursor,
)
from fleet_api.tasks.state_machine import InvalidStateTransition, is_terminal
from fleet_api.workflows.models import Workflow

logger = logging.getLogger(__name__)


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
        new_hash = hashlib.sha256(json.dumps(input_data, sort_keys=True).encode()).hexdigest()

        if existing_hash == new_hash:
            return existing

        raise InputValidationError(
            code=ErrorCode.IDEMPOTENCY_MISMATCH,
            message=(f"Idempotency key '{idempotency_key}' was already used with different input."),
            suggestion=(
                "Use a new idempotency key for different input, or resend the original input."
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
                    "Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks."
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
                tuple_(Task.created_at, Task.id)
                < tuple_(literal(cursor_created_at), literal(cursor_task_id))
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

    async def get_pending_signals(self, agent_id: str) -> list[dict[str, Any]]:
        """Return pending signals for in-flight tasks assigned to *agent_id*.

        Queries for unacknowledged signal events (pause_requested,
        resume_requested, context_injected) on tasks that are currently
        running or paused.  Also includes cancel/redirect signals by checking
        task status transitions.

        This is part of the Phase 2 enhanced sidecar support (Unit 8,
        RFC 1 §7.2 items 5-7).  The sidecar polls ``GET /agents/{id}/tasks/pending``
        which now returns a ``signals`` array alongside pending tasks (Option A —
        extending the existing endpoint rather than creating a new one, because
        the sidecar already polls this endpoint and adding signals to the same
        response avoids doubling poll traffic).

        Returns:
            List of signal dicts, each with: task_id, signal_type, timestamp,
            and optional payload.
        """
        # Signal event types that the sidecar needs to pick up.
        # These are created by pause_task, resume_task, and inject_context.
        signal_event_types = ("pause_requested", "resume_requested", "context_injected")

        # Find tasks assigned to this agent that are in an active (non-terminal,
        # non-accepted) state — these are the tasks the sidecar is currently
        # executing and may need signals for.
        active_statuses = (TaskStatus.RUNNING, TaskStatus.PAUSED)

        active_tasks_stmt = (
            select(Task.id)
            .where(Task.executor_agent_id == agent_id)
            .where(Task.status.in_(active_statuses))
        )
        active_result = await self.session.execute(active_tasks_stmt)
        active_task_ids = [row[0] for row in active_result.all()]

        if not active_task_ids:
            return []

        # Query for signal events on active tasks.  We return all signal events
        # and let the sidecar deduplicate by (task_id, signal_type, timestamp).
        # This is simpler and more resilient than server-side ack tracking.
        signals_stmt = (
            select(TaskEvent)
            .where(TaskEvent.task_id.in_(active_task_ids))
            .where(TaskEvent.event_type.in_(signal_event_types))
            .order_by(TaskEvent.created_at.asc())
        )
        signals_result = await self.session.execute(signals_stmt)
        signal_events = list(signals_result.scalars().all())

        signals: list[dict[str, Any]] = []
        for evt in signal_events:
            signal_item: dict[str, Any] = {
                "task_id": evt.task_id,
                "signal_type": evt.event_type,
                "timestamp": evt.created_at.isoformat() if evt.created_at else "",
            }
            if evt.data:
                signal_item["payload"] = evt.data
            signals.append(signal_item)

        # Also check for tasks that were just redirected or cancelled by the
        # principal — these don't have separate signal events; the task status
        # itself has changed.  The sidecar needs to know so it can stop execution.
        terminal_statuses = (TaskStatus.CANCELLED, TaskStatus.REDIRECTED)
        recently_terminated_stmt = (
            select(Task)
            .where(Task.executor_agent_id == agent_id)
            .where(Task.status.in_(terminal_statuses))
            .where(Task.completed_at.isnot(None))
            # Only return tasks completed in the last 60 seconds to avoid
            # returning stale cancellations from previous sessions.
            .where(Task.completed_at >= func.now() - literal(60).op("* interval '1 second'"))
        )
        terminated_result = await self.session.execute(recently_terminated_stmt)
        terminated_tasks = list(terminated_result.scalars().all())

        for task in terminated_tasks:
            if task.status == TaskStatus.CANCELLED:
                signals.append(
                    {
                        "task_id": task.id,
                        "signal_type": "cancel_requested",
                        "timestamp": (task.completed_at.isoformat() if task.completed_at else ""),
                    }
                )
            elif task.status == TaskStatus.REDIRECTED:
                signals.append(
                    {
                        "task_id": task.id,
                        "signal_type": "redirect_requested",
                        "timestamp": (task.completed_at.isoformat() if task.completed_at else ""),
                    }
                )

        return signals
