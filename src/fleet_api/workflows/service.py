"""Workflow business logic."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.errors import (
    AuthError,
    ConflictError,
    ErrorCode,
    InputValidationError,
    NotFoundError,
)
from fleet_api.workflows.models import Workflow, WorkflowStatus

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

# Alphanumeric + hyphens, max 128 chars
WORKFLOW_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,127}$")


def validate_workflow_id(workflow_id: str) -> None:
    """Validate workflow ID format: non-empty, max 128, alphanumeric+hyphens."""
    if not workflow_id:
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message="Workflow ID must not be empty.",
            suggestion="Provide a non-empty workflow ID using alphanumeric characters and hyphens.",
        )
    if not WORKFLOW_ID_PATTERN.match(workflow_id):
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message=(
                f"Invalid workflow ID '{workflow_id}'. "
                "Must be 1-128 characters, alphanumeric and hyphens only, "
                "starting with an alphanumeric character."
            ),
            suggestion="Use a format like 'my-workflow-name' or 'codeReview'.",
        )


def validate_json_schema_field(schema: dict[str, Any] | None, field_name: str) -> None:
    """Basic structural validation: must be a dict with a 'type' key if provided."""
    if schema is None:
        return
    if not isinstance(schema, dict):
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message=f"{field_name} must be a JSON object.",
            suggestion=f"Provide {field_name} as a JSON object with at least a 'type' key.",
        )
    if "type" not in schema:
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message=f"{field_name} must contain a 'type' key.",
            suggestion=f"Provide {field_name} as a JSON Schema object, e.g. "
            '{"type": "object", "properties": {...}}.',
        )


# ---------------------------------------------------------------------------
# Cursor pagination helpers
# ---------------------------------------------------------------------------


def encode_cursor(workflow_id: str) -> str:
    """Encode a workflow ID into an opaque base64 cursor."""
    return base64.b64encode(json.dumps({"id": workflow_id}).encode()).decode()


def decode_cursor(cursor: str) -> str:
    """Decode an opaque base64 cursor to extract the workflow ID."""
    try:
        data = json.loads(base64.b64decode(cursor))
        return str(data["id"])
    except (ValueError, KeyError) as e:
        # base64.b64decode raises binascii.Error (ValueError subclass)
        # json.loads raises json.JSONDecodeError (ValueError subclass)
        # data["id"] raises KeyError
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message="Invalid pagination cursor.",
            suggestion="Use the cursor value returned from a previous list response.",
        ) from e


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class WorkflowService:
    """CRUD operations for workflows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_workflow(
        self,
        workflow_id: str,
        owner_agent_id: str,
        name: str,
        description: str | None = None,
        tags: list[str] | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        result_retention_days: int = 30,
    ) -> Workflow:
        """Create a new workflow. Raises ConflictError if ID is taken by another owner."""
        validate_workflow_id(workflow_id)
        validate_json_schema_field(input_schema, "input_schema")
        validate_json_schema_field(output_schema, "output_schema")

        # Check for existing workflow with same ID
        existing = await self.session.get(Workflow, workflow_id)
        if existing is not None:
            if existing.owner_agent_id != owner_agent_id:
                raise ConflictError(
                    code=ErrorCode.WORKFLOW_EXISTS,
                    message=f"Workflow '{workflow_id}' already exists.",
                    suggestion="Choose a different workflow ID or contact the workflow owner.",
                    links={"existing": {"href": f"/workflows/{workflow_id}"}},
                )
            # Same owner re-registering: return the existing workflow
            # (idempotent behavior)
            return existing

        workflow = Workflow(
            id=workflow_id,
            name=name,
            owner_agent_id=owner_agent_id,
            description=description,
            tags=tags,
            input_schema=input_schema,
            output_schema=output_schema,
            timeout_seconds=timeout_seconds,
            result_retention_days=result_retention_days,
            status=WorkflowStatus.ACTIVE,
            created_at=datetime.now(UTC),
        )
        self.session.add(workflow)
        await self.session.commit()
        await self.session.refresh(workflow)
        return workflow

    async def get_workflow(self, workflow_id: str) -> Workflow:
        """Get a single workflow by ID. Raises NotFoundError if not found."""
        workflow = await self.session.get(Workflow, workflow_id)
        if workflow is None:
            raise NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message=f"Workflow '{workflow_id}' not found.",
                suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
                links={"list": {"href": "/workflows"}},
            )
        return workflow

    async def list_workflows(
        self,
        status: str | None = None,
        owner: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[Workflow], str | None, bool, int]:
        """List workflows with filtering and cursor pagination.

        Returns (workflows, next_cursor, has_more, total_count).
        """
        base_stmt = select(Workflow).order_by(Workflow.id)

        # Apply filters
        if status is not None:
            try:
                status_enum = WorkflowStatus(status)
            except ValueError:
                raise InputValidationError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid status filter '{status}'.",
                    suggestion="Use 'active' or 'deprecated'.",
                )
            base_stmt = base_stmt.where(Workflow.status == status_enum)
        else:
            # Default to active
            base_stmt = base_stmt.where(Workflow.status == WorkflowStatus.ACTIVE)

        if owner is not None:
            base_stmt = base_stmt.where(Workflow.owner_agent_id == owner)

        if tag is not None:
            # JSONB contains — check if tags array contains the value
            base_stmt = base_stmt.where(Workflow.tags.op("@>")(json.dumps([tag])))

        # Total count (before cursor/limit, after filters)
        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total_count = (await self.session.execute(count_stmt)).scalar_one()

        # Apply cursor
        stmt = base_stmt
        if cursor is not None:
            cursor_id = decode_cursor(cursor)
            stmt = stmt.where(Workflow.id > cursor_id)

        # Fetch limit + 1 to determine has_more
        stmt = stmt.limit(limit + 1)
        result = await self.session.execute(stmt)
        workflows = list(result.scalars().all())

        has_more = len(workflows) > limit
        if has_more:
            workflows = workflows[:limit]

        next_cursor: str | None = None
        if has_more and workflows:
            next_cursor = encode_cursor(workflows[-1].id)

        return workflows, next_cursor, has_more, total_count

    async def update_workflow(
        self,
        workflow_id: str,
        caller_agent_id: str,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
        result_retention_days: int | None = None,
        status: str | None = None,
        # Sentinel to distinguish "not provided" from "set to None"
        _provided_fields: set[str] | None = None,
    ) -> Workflow:
        """Update workflow metadata. Only the owner can update.

        _provided_fields is the set of field names actually provided in the request,
        so we can distinguish between "field not in request" vs "field set to null".
        """
        workflow = await self.get_workflow(workflow_id)

        # Authorization: only the owner can update
        if workflow.owner_agent_id != caller_agent_id:
            raise AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the workflow owner can update this workflow.",
                suggestion="Contact the workflow owner to make changes.",
            )

        provided = _provided_fields or set()

        if "name" in provided and name is not None:
            workflow.name = name
        if "description" in provided:
            workflow.description = description
        if "tags" in provided:
            workflow.tags = tags
        if "input_schema" in provided:
            validate_json_schema_field(input_schema, "input_schema")
            workflow.input_schema = input_schema
        if "output_schema" in provided:
            validate_json_schema_field(output_schema, "output_schema")
            workflow.output_schema = output_schema
        if "timeout_seconds" in provided:
            workflow.timeout_seconds = timeout_seconds
        if "result_retention_days" in provided and result_retention_days is not None:
            workflow.result_retention_days = result_retention_days
        if "status" in provided and status is not None:
            try:
                workflow.status = WorkflowStatus(status)
            except ValueError:
                raise InputValidationError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Invalid status '{status}'.",
                    suggestion="Use 'active' or 'deprecated'.",
                )

        workflow.updated_at = datetime.now(UTC)
        await self.session.commit()
        await self.session.refresh(workflow)
        return workflow
