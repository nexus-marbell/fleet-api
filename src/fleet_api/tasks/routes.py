"""Task API routes — dispatch, read, list, cancel, and sidecar event endpoints.

Routes use full paths (e.g. /workflows/{workflow_id}/run) because task
endpoints nest under /workflows/{id}/ per the RFC.
The router is mounted WITHOUT a prefix in app.py.

The sidecar event endpoint (POST /tasks/{task_id}/events) uses a flat path
because the sidecar only knows task_id, not the workflow.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.config import settings
from fleet_api.database.connection import get_session
from fleet_api.middleware.auth import AuthenticatedAgent, require_auth
from fleet_api.tasks.models import Task
from fleet_api.tasks.service import (
    TaskService,
    build_lineage_chain,
    build_task_links,
    cancel_task,
    count_context_injections,
    inject_context,
    pause_task,
    process_sidecar_event,
    redirect_task,
    resume_task,
    retask_task,
    task_to_detail_response,
    task_to_summary_response,
)
from fleet_api.tasks.sse import stream_task_events

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
        description="Webhook URL for signed result delivery on task completion or failure",
    )


class TaskCancelRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/cancel."""

    reason: str | None = None


class RetaskRefinement(BaseModel):
    """Refinement instructions for retasking."""

    message: str = Field(..., description="What is wrong or missing")
    additional_input: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None


class TaskRetaskRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/retask."""

    refinement: RetaskRefinement
    priority: str | None = None


class TaskPauseRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/pause."""

    reason: str | None = None


class TaskResumeRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/resume."""

    priority: str | None = Field(
        None,
        description="Priority override: low, normal, high, critical",
    )  # Extends RFC §3.12 with 'critical' to match TaskPriority enum used across all endpoints


class TaskRedirectRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/redirect."""

    reason: str = Field(..., description="Why the redirect is needed")
    new_input: dict[str, Any] = Field(..., description="Input for the new task (replaces original)")
    inherit_progress: bool = Field(
        False, description="Whether new task inherits progress metadata"
    )
    priority: str | None = Field(
        None,
        description="Priority for new task. If omitted, inherits from original.",
    )


class ContextPayload(BaseModel):
    """Payload for context injection per RFC §3.13."""

    message: str = Field(..., description="Human-readable description of the context")
    data: dict[str, Any] | None = Field(None, description="Optional structured data")


class ContextInjectionRequest(BaseModel):
    """Request body for POST /workflows/{workflow_id}/tasks/{task_id}/context."""

    context_type: Literal["additional_input", "constraint", "correction", "reference"] = Field(
        ...,
        description="One of: additional_input, constraint, correction, reference",
    )
    payload: ContextPayload
    sequence: int = Field(
        ...,
        description="Monotonically increasing context sequence number",
        gt=0,
    )
    urgency: Literal["low", "normal", "immediate"] = Field(
        "normal",
        description="Urgency level: low, normal, immediate",
    )


class TaskEventRequest(BaseModel):
    """Request body for POST /tasks/{task_id}/events (sidecar)."""

    event_type: str = Field(
        ..., description="Event type: status, progress, log, completed, failed, heartbeat"
    )
    data: dict[str, Any] = Field(
        default_factory=dict, description="Event payload"
    )
    sequence: int = Field(..., description="Monotonically increasing sequence number", gt=0)


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

    Cancelled is a terminal state — self, workflow, and rerun.
    Action links include ``"method": "POST"`` per RFC section 3.6.
    """
    return {
        "self": {"href": f"/workflows/{workflow_id}/tasks/{task_id}"},
        "workflow": {"href": f"/workflows/{workflow_id}"},
        "rerun": {"method": "POST", "href": f"/workflows/{workflow_id}/run"},
    }


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _cancel_response(
    task: Task,
    cancelled_by: str,
    reason: str | None,
) -> dict[str, Any]:
    """Build the cancel response — returns the full updated task per RFC.

    Uses RFC field names: ``caller`` (not principal_agent_id),
    ``executor`` (not executor_agent_id).

    Includes ``cancelled_at``, ``cancelled_by``, and ``reason`` at top level
    per Issue #16 spec.
    """
    return {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "caller": task.principal_agent_id,
        "executor": task.executor_agent_id,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "input": task.input,
        "result": task.result,
        "priority": task.priority.value if hasattr(task.priority, "value") else str(task.priority),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
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
    agent: AuthenticatedAgent = Depends(require_auth),
    service: TaskService = Depends(get_task_service),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> Any:
    """Dispatch a task to a workflow.

    Returns 202 Accepted with a task handle for new tasks.
    Returns 200 OK for idempotent replays (same key + same input).
    """
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
        callback_url=body.callback_url,
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
    agent: AuthenticatedAgent = Depends(require_auth),
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """Get a single task by ID within a workflow."""
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
    agent: AuthenticatedAgent = Depends(require_auth),
    service: TaskService = Depends(get_task_service),
) -> dict[str, Any]:
    """List tasks for a workflow with filtering and cursor pagination."""
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
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cancel a task. Caller must be the task principal or workflow owner."""
    reason = body.reason if body is not None else None

    task = await cancel_task(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        cancelled_by=agent.agent_id,
        reason=reason,
    )

    return _cancel_response(task, cancelled_by=agent.agent_id, reason=reason)


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/tasks/{task_id}/retask — Phase 2 Unit 5
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/tasks/{task_id}/retask", status_code=201)
async def retask_task_endpoint(
    workflow_id: str,
    task_id: str,
    body: TaskRetaskRequest,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Retask a completed or failed task with refinement instructions.

    Creates a new task linked to the original with lineage tracking.
    Returns 201 Created with the new task and lineage information.
    """
    # TODO(Phase 2 Wave 3): Idempotency-Key support deferred — see Issue #44
    refinement_dict = body.refinement.model_dump(exclude_none=True)

    new_task, original_task = await retask_task(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        caller_agent_id=agent.agent_id,
        refinement=refinement_dict,
        priority=body.priority,
    )

    # Build lineage chain
    chain = await build_lineage_chain(session, new_task)

    # Count context injections on original task
    injected_count = await count_context_injections(session, original_task.id)

    # Build response
    status_value = (
        new_task.status.value
        if hasattr(new_task.status, "value")
        else str(new_task.status)
    )
    priority_value = (
        new_task.priority.value
        if hasattr(new_task.priority, "value")
        else str(new_task.priority)
    )

    response_data: dict[str, Any] = {
        "task_id": new_task.id,
        "parent_task_id": new_task.parent_task_id,
        "workflow_id": new_task.workflow_id,
        "status": status_value,
        "caller": new_task.principal_agent_id,
        "executor": new_task.executor_agent_id,
        "priority": priority_value,
        "created_at": new_task.created_at.isoformat() if new_task.created_at else None,
        "lineage": {
            "depth": new_task.lineage_depth,
            "root_task_id": new_task.root_task_id,
            "chain": chain,
        },
        "inherited_context": {
            "original_input": True,
            "original_result": original_task.result is not None,
            "injected_contexts": injected_count,
        },
        "_links": build_task_links(new_task.id, workflow_id, status_value),
    }

    # Add parent link
    # RFC §3.14 shows bare string; using HATEOAS object form per Agentic API Standard §2
    response_data["_links"]["parent"] = {
        "href": f"/workflows/{workflow_id}/tasks/{original_task.id}",
    }

    return JSONResponse(status_code=201, content=response_data)


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/tasks/{task_id}/redirect — Phase 2 Unit 6
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/tasks/{task_id}/redirect", status_code=201)
async def redirect_task_endpoint(
    workflow_id: str,
    task_id: str,
    body: TaskRedirectRequest,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Redirect a running or paused task with new input.

    Transitions the original task to REDIRECTED and creates a new task
    with the provided new_input. Returns 201 Created with the new task
    and lineage information.
    """
    # TODO(Phase 2 Wave 3): Idempotency-Key support deferred — see Issue #44

    new_task, original_task = await redirect_task(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        caller_agent_id=agent.agent_id,
        reason=body.reason,
        new_input=body.new_input,
        inherit_progress=body.inherit_progress,
        priority=body.priority,
    )

    # Build lineage chain
    chain = await build_lineage_chain(session, new_task)

    # Build response per RFC §3.15
    status_value = (
        new_task.status.value
        if hasattr(new_task.status, "value")
        else str(new_task.status)
    )
    priority_value = (
        new_task.priority.value
        if hasattr(new_task.priority, "value")
        else str(new_task.priority)
    )

    response_data: dict[str, Any] = {
        "task_id": new_task.id,
        "workflow_id": new_task.workflow_id,
        "status": status_value,
        "redirected_from": original_task.id,
        "lineage": {
            "depth": new_task.lineage_depth,
            "root_task_id": new_task.root_task_id,
            "chain": chain,
        },
        "caller": new_task.principal_agent_id,
        "executor": new_task.executor_agent_id,
        "priority": priority_value,
        "created_at": new_task.created_at.isoformat() if new_task.created_at else None,
        "_links": build_task_links(new_task.id, workflow_id, status_value),
    }

    # Add redirected_from link per RFC §3.15
    # RFC §3.15 shows bare string links; using HATEOAS object form per Agentic API Standard §2
    response_data["_links"]["redirected_from"] = {
        "href": f"/workflows/{workflow_id}/tasks/{original_task.id}",
    }

    return JSONResponse(status_code=201, content=response_data)


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/tasks/{task_id}/pause — Phase 2 Unit 2+3
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/tasks/{task_id}/pause")
async def pause_task_endpoint(
    workflow_id: str,
    task_id: str,
    body: TaskPauseRequest | None = None,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Pause a running task. Caller must be the task principal or workflow owner."""
    reason = body.reason if body is not None else None

    task, event = await pause_task(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        paused_by=agent.agent_id,
        reason=reason,
    )

    return _pause_response(task, event)


def _pause_response(task: Task, event: Any) -> dict[str, Any]:
    """Build the RFC §3.11 pause response."""
    pause_ttl = settings.fleet_pause_ttl_seconds
    paused_at = task.paused_at
    expires_at = paused_at + timedelta(seconds=pause_ttl) if paused_at else None

    progress = task.metadata_.get("progress", 0) if task.metadata_ else 0
    progress_message = task.metadata_.get("progress_message") if task.metadata_ else None

    paused_state = {
        "progress": progress,
        "message": progress_message,
        "resumable": True,
        "state_ttl_seconds": pause_ttl,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }

    return {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "paused_at": paused_at.isoformat() if paused_at else None,
        "paused_state": paused_state,
        "_links": build_task_links(task.id, task.workflow_id, task.status),
    }


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/tasks/{task_id}/resume — Phase 2 Unit 2+3
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/tasks/{task_id}/resume")
async def resume_task_endpoint(
    workflow_id: str,
    task_id: str,
    body: TaskResumeRequest | None = None,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Resume a paused task. Caller must be the task principal or workflow owner."""
    priority = body.priority if body is not None else None

    task, event = await resume_task(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        resumed_by=agent.agent_id,
        priority=priority,
    )

    return _resume_response(task, event)


def _resume_response(task: Task, event: Any) -> dict[str, Any]:
    """Build the RFC §3.12 resume response."""
    paused_duration_seconds = event.data.get("paused_duration_seconds", 0) if event.data else 0
    progress = task.metadata_.get("progress", 0) if task.metadata_ else 0

    return {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "resumed_at": event.created_at.isoformat() if event.created_at else None,
        "paused_duration_seconds": paused_duration_seconds,
        "progress": progress,
        "_links": build_task_links(task.id, task.workflow_id, task.status),
    }


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/tasks/{task_id}/context — Phase 2 Unit 4
# ---------------------------------------------------------------------------


@router.post("/workflows/{workflow_id}/tasks/{task_id}/context", status_code=202)
async def inject_context_endpoint(
    workflow_id: str,
    task_id: str,
    body: ContextInjectionRequest,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Inject additional context into a running or paused task.

    Returns 202 Accepted with the context injection details.
    Rejects out-of-sequence injections with 409 CONTEXT_REJECTED.
    """
    payload_dict = body.payload.model_dump(exclude_none=True)

    result = await inject_context(
        session=session,
        workflow_id=workflow_id,
        task_id=task_id,
        caller_agent_id=agent.agent_id,
        context_type=body.context_type,
        payload=payload_dict,
        sequence=body.sequence,
        urgency=body.urgency,
    )

    return JSONResponse(status_code=202, content=result)


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/events (AUTHENTICATED — sidecar) — Issue #17
# ---------------------------------------------------------------------------


@router.post("/tasks/{task_id}/events", status_code=201)
async def post_task_event(
    task_id: str,
    body: TaskEventRequest,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Receive an execution event from the sidecar.

    Updates task status, progress, or result based on event_type.
    Only the task's executor agent may post events.
    """
    event, task = await process_sidecar_event(
        session=session,
        task_id=task_id,
        event_type=body.event_type,
        data=body.data,
        sequence=body.sequence,
        executor_agent_id=agent.agent_id,
    )

    return {
        "received": True,
        "event_id": event.id,
        "task_id": task.id,
        "event_type": event.event_type,
        "sequence": event.sequence,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        # HATEOAS _links: object form {"href": ...} per Agentic API Standard §2
        # (spec showed plain string — implementation is more correct)
        "_links": {
            "task": {"href": f"/workflows/{task.workflow_id}/tasks/{task.id}"},
            "events": {"href": f"/tasks/{task.id}/events"},
        },
    }


# ---------------------------------------------------------------------------
# GET /workflows/{workflow_id}/tasks/{task_id}/stream (SSE) — Phase 2 Unit 1
# ---------------------------------------------------------------------------

router.add_api_route(
    "/workflows/{workflow_id}/tasks/{task_id}/stream",
    stream_task_events,
    methods=["GET"],
    tags=["tasks"],
    summary="Stream task events via SSE",
    description=(
        "Server-Sent Events stream for task lifecycle events. "
        "Supports reconnection via Last-Event-Id header."
    ),
)
