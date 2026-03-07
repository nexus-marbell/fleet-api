"""Task business logic — creation, dispatch, read, and list operations."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.agents.models import Agent, AgentStatus
from fleet_api.errors import (
    ErrorCode,
    InfrastructureError,
    InputValidationError,
    NotFoundError,
)
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus
from fleet_api.workflows.models import Workflow

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDEMPOTENCY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# HATEOAS link builder (shared with Issues #15, #16)
# ---------------------------------------------------------------------------


def build_task_links(task_id: str, workflow_id: str, status: TaskStatus | str) -> dict[str, Any]:
    """Build state-dependent HATEOAS links for a task (RFC section 3.6).

    All links use {"href": "..."} format for HATEOAS compliance.
    Status-dependent action links per spec:
      - accepted: cancel
      - running: cancel, pause, context, redirect
      - paused: cancel, resume
      - completed: retask, rerun
      - failed: retask, rerun
      - cancelled/retasked/redirected: no action links (self + workflow only)
    """
    if not isinstance(status, TaskStatus):
        status = TaskStatus(status)

    base = f"/workflows/{workflow_id}/tasks/{task_id}"
    links: dict[str, Any] = {
        "self": {"href": base},
    }

    if status == TaskStatus.ACCEPTED:
        links["cancel"] = {"href": f"{base}/cancel"}

    elif status == TaskStatus.RUNNING:
        links["cancel"] = {"href": f"{base}/cancel"}
        links["pause"] = {"href": f"{base}/pause"}
        links["context"] = {"href": f"{base}/context"}
        links["redirect"] = {"href": f"{base}/redirect"}

    elif status == TaskStatus.PAUSED:
        links["cancel"] = {"href": f"{base}/cancel"}
        links["resume"] = {"href": f"{base}/resume"}

    elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        links["retask"] = {"href": f"{base}/retask"}
        links["rerun"] = {"href": f"/workflows/{workflow_id}/run"}

    # cancelled, retasked, redirected: no action links

    links["workflow"] = {"href": f"/workflows/{workflow_id}"}
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
            .order_by(Task.created_at.desc())
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

        stmt = base_stmt
        if cursor is not None:
            _cursor_task_id, cursor_created_at = decode_task_cursor(cursor)
            stmt = stmt.where(Task.created_at < cursor_created_at)

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
