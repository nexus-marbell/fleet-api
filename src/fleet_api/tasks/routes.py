"""Task API routes — dispatch, read, list, and cancel endpoints.

Routes use full paths (e.g. /workflows/{workflow_id}/run) because task
endpoints nest under /workflows/{id}/ per the RFC.
The router is mounted WITHOUT a prefix in app.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.database.connection import get_session
from fleet_api.middleware.auth import AuthenticatedAgent, require_auth
from fleet_api.tasks.service import (
    TaskService,
    cancel_task,
    task_to_detail_response,
    task_to_summary_response,
)

router = APIRouter(tags=["tasks"])


# ---------------------------------------------------------------------------
# Pydantic request schemas
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
    callback_url: str | None = Field(
        None,
        description="Webhook URL for result delivery — delivery in Phase 2",
    )


class TaskCancelRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/cancel."""

    reason: str | None = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_task_service(
    session: AsyncSession = Depends(get_session),
) -> TaskService:
    """FastAPI dependency: instantiate TaskService with a database session."""
    return TaskService(session)


# ---------------------------------------------------------------------------
# HATEOAS link builders
# ---------------------------------------------------------------------------


def build_cancel_links(task_id: str, workflow_id: str) -> dict[str, Any]:
    """Build HATEOAS _links for a cancelled task response.

    Cancelled is a terminal state — only self + workflow links, no action links.
    """
    return {
        "self": {"href": f"/workflows/{workflow_id}/tasks/{task_id}"},
        "workflow": {"href": f"/workflows/{workflow_id}"},
        "rerun": {"method": "POST", "href": f"/workflows/{workflow_id}/run"},
    }


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _cancel_response(task: Any, cancelled_by: str, reason: str | None) -> dict[str, Any]:
    """Build the cancel response matching RFC section 3.16 field names."""
    return {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "status": "cancelled",
        "cancelled_at": task.completed_at.isoformat() if task.completed_at else None,
        "cancelled_by": cancelled_by,
        "reason": reason,
        "_links": build_cancel_links(task.id, task.workflow_id),
    }


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

    effective_idempotency_key = idempotency_key or body.idempotency_key

    task, workflow, is_replay = await service.create_task(
        workflow_id=workflow_id,
        caller_agent_id=agent.agent_id,
        input_data=body.input,
        executor_agent_id=body.executor,
        priority=body.priority,
        timeout_seconds=body.timeout_seconds,
        idempotency_key=effective_idempotency_key,
        metadata=body.metadata,
    )

    response_data = service.build_task_response(
        task=task,
        workflow=workflow,
        is_replay=is_replay,
        idempotency_key=effective_idempotency_key,
    )

    if is_replay:
        return JSONResponse(status_code=200, content=response_data)

    return JSONResponse(status_code=202, content=response_data)


@router.get("/workflows/{workflow_id}/tasks/{task_id}")
async def get_task(
    workflow_id: str,
    task_id: str,
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """Get a single task by ID within a workflow."""
    if agent is None:
        raise RuntimeError("require_auth dependency returned None on a protected route")
    task = await service.get_task(workflow_id, task_id)
    return task_to_detail_response(task)


@router.get("/workflows/{workflow_id}/tasks")
async def list_tasks(
    workflow_id: str,
    status: str | None = Query(None, description="Filter by task status"),
    priority: str | None = Query(None, description="Filter by task priority"),
    caller: str | None = Query(None, description="Filter by calling agent ID"),
    since: str | None = Query(None, description="ISO 8601 start time (inclusive)"),
    until: str | None = Query(None, description="ISO 8601 end time (inclusive)"),
    limit: int = Query(20, ge=1, le=100, description="Max results per page"),
    cursor: str | None = Query(None, description="Pagination cursor from previous response"),
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """List tasks for a workflow with filtering and cursor pagination."""
    if agent is None:
        raise RuntimeError("require_auth dependency returned None on a protected route")

    tasks, next_cursor, has_more, total_count = await service.list_tasks(
        workflow_id=workflow_id,
        status=status,
        priority=priority,
        caller=caller,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )

    data = [task_to_summary_response(t) for t in tasks]

    params: list[str] = []
    if status is not None:
        params.append(f"status={status}")
    if priority is not None:
        params.append(f"priority={priority}")
    if caller is not None:
        params.append(f"caller={caller}")
    if since is not None:
        params.append(f"since={since}")
    if until is not None:
        params.append(f"until={until}")
    params.append(f"limit={limit}")
    self_href = f"/workflows/{workflow_id}/tasks"
    if params:
        self_href += "?" + "&".join(params)

    response_links: dict[str, Any] = {
        "self": {"href": self_href},
        "workflow": {"href": f"/workflows/{workflow_id}"},
    }
    if next_cursor:
        response_links["next"] = {
            "href": f"/workflows/{workflow_id}/tasks?cursor={next_cursor}&limit={limit}"
        }

    response: dict[str, Any] = {
        "data": data,
        "pagination": {
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total_count": total_count,
            "limit": limit,
        },
        "_links": response_links,
    }
    return response


@router.post("/workflows/{workflow_id}/tasks/{task_id}/cancel")
async def cancel_task_endpoint(
    workflow_id: str,
    task_id: str,
    body: TaskCancelRequest | None = None,
    agent: AuthenticatedAgent | None = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cancel a task. Caller must be the task principal or workflow owner."""
    if agent is None:
        raise RuntimeError("require_auth dependency returned None on a protected route")

    reason = body.reason if body is not None else None

    task = await cancel_task(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        cancelled_by=agent.agent_id,
        reason=reason,
    )

    return _cancel_response(task, cancelled_by=agent.agent_id, reason=reason)
