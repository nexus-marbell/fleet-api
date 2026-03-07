"""Workflow API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.database.connection import get_session
from fleet_api.middleware.auth import AuthenticatedAgent, require_auth
from fleet_api.workflows.models import Workflow, WorkflowStatus
from fleet_api.workflows.service import WorkflowService

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic request schemas
# ---------------------------------------------------------------------------


class WorkflowCreateRequest(BaseModel):
    """Request body for POST /workflows."""

    id: str = Field(..., description="Unique workflow identifier", max_length=128)
    name: str | None = Field(
        None, description="Human-readable workflow name", max_length=256
    )
    description: str | None = Field(None, description="Workflow description")
    tags: list[str] | None = Field(None, description="Workflow tags for discovery")
    input_schema: dict[str, Any] | None = Field(
        None, description="JSON Schema for workflow input"
    )
    output_schema: dict[str, Any] | None = Field(
        None, description="JSON Schema for workflow output"
    )
    timeout_seconds: int | None = Field(
        None, description="Workflow timeout in seconds", gt=0
    )
    result_retention_days: int = Field(30, description="Days to retain results", gt=0)


class WorkflowUpdateRequest(BaseModel):
    """Request body for PUT /workflows/{workflow_id}."""

    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    result_retention_days: int | None = Field(None, gt=0)
    status: str | None = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_workflow_service(
    session: AsyncSession = Depends(get_session),
) -> WorkflowService:
    """FastAPI dependency: instantiate WorkflowService with a database session."""
    return WorkflowService(session)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workflow_to_response(workflow: Workflow) -> dict[str, Any]:
    """Convert a Workflow model to a response dict with _links."""
    return {
        "id": workflow.id,
        "name": workflow.name,
        "owner_agent_id": workflow.owner_agent_id,
        "description": workflow.description,
        "tags": workflow.tags,
        "input_schema": workflow.input_schema,
        "output_schema": workflow.output_schema,
        "timeout_seconds": workflow.timeout_seconds,
        "result_retention_days": workflow.result_retention_days,
        "status": (
            workflow.status.value
            if isinstance(workflow.status, WorkflowStatus)
            else str(workflow.status)
        ),
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
        "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
        "_links": {
            "self": {"href": f"/workflows/{workflow.id}"},
            "update": {"href": f"/workflows/{workflow.id}", "method": "PUT"},
            "runs": {"href": f"/tasks?workflow_id={workflow.id}"},
            "owner": {"href": f"/agents/{workflow.owner_agent_id}"},
        },
    }


def _workflow_links() -> dict[str, Any]:
    """Common _links for workflow list responses."""
    return {
        "self": {"href": "/workflows"},
        "create": {"href": "/workflows", "method": "POST"},
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def create_workflow(
    body: WorkflowCreateRequest,
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Create a new workflow. Owner = authenticated agent."""
    assert agent is not None  # Protected route -- always authenticated
    workflow = await service.create_workflow(
        workflow_id=body.id,
        owner_agent_id=agent.agent_id,
        name=body.name,
        description=body.description,
        tags=body.tags,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        timeout_seconds=body.timeout_seconds,
        result_retention_days=body.result_retention_days,
    )
    response = _workflow_to_response(workflow)
    # Pattern 13: onboarding steps for newly created workflow
    response["onboarding"] = {
        "steps": [
            {
                "step": 1,
                "action": "Submit a task",
                "endpoint": "POST /tasks",
                "description": f"Submit a task using workflow_id='{workflow.id}'.",
            },
            {
                "step": 2,
                "action": "Monitor task status",
                "endpoint": "GET /tasks/{task_id}",
                "description": "Poll the task endpoint for status updates.",
            },
            {
                "step": 3,
                "action": "Retrieve results",
                "endpoint": "GET /tasks/{task_id}",
                "description": "Fetch completed task results from the task endpoint.",
            },
        ]
    }
    return response


@router.get("")
async def list_workflows(
    status: str | None = Query(
        None, description="Filter by status (active/deprecated)"
    ),
    owner: str | None = Query(None, description="Filter by owner agent ID"),
    tag: str | None = Query(None, description="Filter by tag"),
    limit: int = Query(20, ge=1, le=100, description="Number of results per page"),
    cursor: str | None = Query(
        None, description="Pagination cursor from previous response"
    ),
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """List workflows with filtering and cursor pagination."""
    assert agent is not None  # Protected route
    workflows, next_cursor, has_more = await service.list_workflows(
        status=status,
        owner=owner,
        tag=tag,
        limit=limit,
        cursor=cursor,
    )
    data = [_workflow_to_response(w) for w in workflows]
    response: dict[str, Any] = {
        "data": data,
        "pagination": {
            "next_cursor": next_cursor,
            "has_more": has_more,
        },
        "_links": _workflow_links(),
    }
    if next_cursor:
        response["_links"]["next"] = {
            "href": f"/workflows?cursor={next_cursor}&limit={limit}"
        }
    return response


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Get a single workflow by ID."""
    assert agent is not None  # Protected route
    workflow = await service.get_workflow(workflow_id)
    return _workflow_to_response(workflow)


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdateRequest,
    agent: AuthenticatedAgent | None = Depends(require_auth),
    service: WorkflowService = Depends(get_workflow_service),
) -> dict[str, Any]:
    """Update workflow metadata. Owner only."""
    assert agent is not None  # Protected route

    # Determine which fields were actually provided in the request body
    # (to distinguish "not provided" from "set to null")
    provided_fields: set[str] = set(body.model_fields_set)

    workflow = await service.update_workflow(
        workflow_id=workflow_id,
        caller_agent_id=agent.agent_id,
        name=body.name,
        description=body.description,
        tags=body.tags,
        input_schema=body.input_schema,
        output_schema=body.output_schema,
        timeout_seconds=body.timeout_seconds,
        result_retention_days=body.result_retention_days,
        status=body.status,
        _provided_fields=provided_fields,
    )
    return _workflow_to_response(workflow)
