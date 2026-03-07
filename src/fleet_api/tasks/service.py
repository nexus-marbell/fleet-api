"""Task business logic — creation, idempotency, input validation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
from sqlalchemy import select
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


def build_task_links(task_id: str, workflow_id: str, status: str) -> dict[str, Any]:
    """Build HATEOAS _links for a task resource.

    The set of available actions depends on the current task status.
    For "accepted" status: self, stream, pause, cancel, context, workflow.
    """
    base = f"/workflows/{workflow_id}/tasks/{task_id}"
    links: dict[str, Any] = {
        "self": {"href": base},
        "stream": {"href": f"{base}/stream"},
        "workflow": {"href": f"/workflows/{workflow_id}"},
    }

    # Action links depend on status
    if status in ("accepted", "running"):
        links["cancel"] = {"method": "POST", "href": f"{base}/cancel"}
    if status in ("accepted", "running"):
        links["pause"] = {"method": "POST", "href": f"{base}/pause"}
    if status in ("accepted", "running", "paused"):
        links["context"] = {"method": "POST", "href": f"{base}/context"}

    return links


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TaskService:
    """Business logic for task creation and dispatch."""

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

        # Compare input: hash-based comparison for reliability
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
        task was returned via idempotency replay.  The workflow is always
        returned so the caller never needs a second DB fetch.
        """
        # 1. Idempotency check — BEFORE any other work.  On replay, workflow
        #    fetch, suspension check, and input validation are wasted.  A replay
        #    should return the original response regardless of current state.
        if idempotency_key is not None:
            existing = await self._check_idempotency(idempotency_key, input_data)
            if existing is not None:
                workflow = await self.get_workflow_or_404(workflow_id)
                return existing, workflow, True

        # 2. Look up workflow
        workflow = await self.get_workflow_or_404(workflow_id)

        # 3. Resolve effective executor: explicit parameter > workflow owner.
        #    Must be resolved BEFORE suspension check so the check targets the
        #    agent that will actually execute the task.
        effective_executor = executor_agent_id or workflow.owner_agent_id

        # 4. Check effective executor is not suspended
        await self._check_agent_not_suspended(effective_executor)

        # 5. Validate input against workflow's input_schema
        self.validate_input(input_data, workflow.input_schema)

        # 6. Validate priority
        try:
            priority_enum = TaskPriority(priority)
        except ValueError:
            valid = ", ".join(p.value for p in TaskPriority)
            raise InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Invalid priority '{priority}'. Must be one of: {valid}.",
                suggestion=f"Use one of: {valid}.",
            )

        # 7. Create the task
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

        # 8. Create initial TaskEvent
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
        """Build the RFC-compliant task response dict."""
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

        # Idempotency block: only present when an idempotency key was provided
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
