"""Task API routes — dispatch, status, lifecycle.

Routes are defined with full paths (e.g. /workflows/{workflow_id}/run)
because L3 task endpoints nest under /workflows/{id}/ per the RFC.
The router is mounted WITHOUT a prefix in app.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.database.connection import get_session
from fleet_api.middleware.auth import AuthenticatedAgent, require_auth
from fleet_api.tasks.service import TaskService

router = APIRouter(tags=["tasks"])


# ---------------------------------------------------------------------------
# Pydantic request schema
# ---------------------------------------------------------------------------


class TaskRunRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/run."""

    input: dict[str, Any]
    executor: str | None = Field(None, description="Optional executor agent ID")
    priority: str = Field(
        "normal",
        description="Task priority: low, normal, high, critical",
    )
    timeout_seconds: int | None = Field(
        None, description="Override workflow timeout", gt=0
    )
    idempotency_key: str | None = Field(
        None, description="Idempotency key (also accepted as header)"
    )
    metadata: dict[str, Any] | None = Field(None, description="Arbitrary metadata")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_task_service(
    session: AsyncSession = Depends(get_session),
) -> TaskService:
    """FastAPI dependency: instantiate TaskService with a database session."""
    return TaskService(session)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/run", status_code=202)
async def run_task(
    workflow_id: str,
    body: TaskRunRequest,
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: TaskService = Depends(get_task_service),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> Any:
    """Dispatch a task to a workflow.

    Returns 202 Accepted with a task handle for new tasks.
    Returns 200 OK for idempotent replays (same key + same input).
    """
    if agent is None:
        raise RuntimeError(
            "require_auth returned None on a protected route"
        )

    # Header takes precedence over body for idempotency key
    effective_idempotency_key = idempotency_key or body.idempotency_key

    task, is_replay = await service.create_task(
        workflow_id=workflow_id,
        caller_agent_id=agent.agent_id,
        input_data=body.input,
        executor_agent_id=body.executor,
        priority=body.priority,
        timeout_seconds=body.timeout_seconds,
        idempotency_key=effective_idempotency_key,
        metadata=body.metadata,
    )

    # For replay, we still need the workflow for estimated_duration
    workflow = await service.get_workflow_or_404(workflow_id)

    response_data = service.build_task_response(
        task=task,
        workflow=workflow,
        is_replay=is_replay,
        idempotency_key=effective_idempotency_key,
    )

    if is_replay:
        return JSONResponse(status_code=200, content=response_data)

    return JSONResponse(status_code=202, content=response_data)
