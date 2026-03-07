"""Tests for workflow CRUD endpoints.

Uses FastAPI dependency overrides for auth and workflow service so tests
have no real database dependency. All four endpoints are tested:
  POST /workflows
  GET  /workflows
  GET  /workflows/{workflow_id}
  PUT  /workflows/{workflow_id}
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.workflows.models import Workflow, WorkflowStatus
from fleet_api.workflows.routes import get_workflow_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
OTHER_AGENT_ID = "other-agent-002"


def _make_workflow(
    workflow_id: str = "wf-test",
    owner: str = AGENT_ID,
    name: str | None = "Test Workflow",
    description: str | None = "A test workflow",
    tags: list[str] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    timeout_seconds: int | None = 300,
    result_retention_days: int = 30,
    status: WorkflowStatus = WorkflowStatus.ACTIVE,
) -> MagicMock:
    """Create a mock Workflow object."""
    wf = MagicMock(spec=Workflow)
    wf.id = workflow_id
    wf.name = name
    wf.owner_agent_id = owner
    wf.description = description
    wf.tags = tags
    wf.input_schema = input_schema
    wf.output_schema = output_schema
    wf.timeout_seconds = timeout_seconds
    wf.result_retention_days = result_retention_days
    wf.status = status
    wf.created_at = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
    wf.updated_at = None
    return wf


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _create_test_app(
    mock_service: MagicMock, agent_id: str = AGENT_ID
) -> Any:
    """Create a test app with auth and service overrides."""
    app = create_app()

    # Override auth to return a known agent
    async def mock_auth() -> AuthenticatedAgent:
        mock_key = MagicMock(spec=Ed25519PublicKey)
        return AuthenticatedAgent(agent_id=agent_id, public_key=mock_key)

    app.dependency_overrides[require_auth] = mock_auth
    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    app.dependency_overrides[get_workflow_service] = lambda: mock_service
    return app


@pytest.fixture
def mock_service() -> MagicMock:
    """Create a mock WorkflowService."""
    return MagicMock()


@pytest.fixture
def app(mock_service: MagicMock) -> Any:
    return _create_test_app(mock_service)


@pytest.fixture
async def client(app: Any) -> AsyncClient:  # type: ignore[misc]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST /workflows
# ---------------------------------------------------------------------------


class TestCreateWorkflow:
    @pytest.mark.asyncio
    async def test_create_workflow_success(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows with valid data returns 201 with workflow details."""
        mock_wf = _make_workflow()
        mock_service.create_workflow = AsyncMock(return_value=mock_wf)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows",
                json={
                    "id": "wf-test",
                    "name": "Test Workflow",
                    "description": "A test workflow",
                    "tags": ["code-review"],
                    "timeout_seconds": 300,
                    "result_retention_days": 30,
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "wf-test"
        assert data["name"] == "Test Workflow"
        assert data["owner"] == AGENT_ID
        assert data["status"] == "active"
        assert "_links" in data
        assert data["_links"]["self"]["href"] == "/workflows/wf-test"
        # Pattern 13: onboarding steps (flat list per spec)
        assert "onboarding" in data
        assert isinstance(data["onboarding"], list)
        assert len(data["onboarding"]) == 2
        assert data["onboarding"][0]["method"] == "GET"
        assert data["onboarding"][0]["expected_status"] == 200
        assert data["onboarding"][1]["method"] == "POST"
        assert data["onboarding"][1]["expected_status"] == 202

    @pytest.mark.asyncio
    async def test_create_workflow_conflict(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows with existing ID from different owner returns 409."""
        from fleet_api.errors import ConflictError, ErrorCode

        mock_service.create_workflow = AsyncMock(
            side_effect=ConflictError(
                code=ErrorCode.WORKFLOW_EXISTS,
                message="Workflow 'wf-taken' already exists.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows",
                json={"id": "wf-taken", "name": "Taken Workflow", "result_retention_days": 30},
            )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "WORKFLOW_EXISTS"

    @pytest.mark.asyncio
    async def test_create_workflow_invalid_id_empty(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows with empty ID returns 422."""
        from fleet_api.errors import ErrorCode, InputValidationError

        mock_service.create_workflow = AsyncMock(
            side_effect=InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message="Workflow ID must not be empty.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows",
                json={"id": "", "name": "Empty ID Workflow", "result_retention_days": 30},
            )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_create_workflow_with_schemas(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows with input_schema and output_schema returns 201."""
        mock_wf = _make_workflow(
            input_schema={
                "type": "object",
                "properties": {"code": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
            },
        )
        mock_service.create_workflow = AsyncMock(return_value=mock_wf)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows",
                json={
                    "id": "wf-test",
                    "name": "Test Workflow",
                    "input_schema": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                    },
                    "result_retention_days": 30,
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["input_schema"]["type"] == "object"
        assert data["output_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# GET /workflows
# ---------------------------------------------------------------------------


class TestListWorkflows:
    @pytest.mark.asyncio
    async def test_list_workflows_empty(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows with no data returns empty list."""
        mock_service.list_workflows = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []
        assert data["pagination"]["has_more"] is False
        assert data["pagination"]["next_cursor"] is None
        assert data["pagination"]["total_count"] == 0
        assert data["pagination"]["limit"] == 20
        assert "_links" in data

    @pytest.mark.asyncio
    async def test_list_workflows_with_data(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows returns workflow list with pagination."""
        wf1 = _make_workflow(workflow_id="wf-alpha", name="Alpha")
        wf2 = _make_workflow(workflow_id="wf-beta", name="Beta")
        mock_service.list_workflows = AsyncMock(
            return_value=([(wf1, "active"), (wf2, "active")], None, False, 2)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 2
        assert data["data"][0]["id"] == "wf-alpha"
        assert data["data"][1]["id"] == "wf-beta"

    @pytest.mark.asyncio
    async def test_list_workflows_with_pagination(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows with has_more=True includes next cursor."""
        wf1 = _make_workflow(workflow_id="wf-alpha")
        mock_service.list_workflows = AsyncMock(
            return_value=([(wf1, "active")], "eyJpZCI6ICJ3Zi1hbHBoYSJ9", True, 5)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows?limit=1")

        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["has_more"] is True
        assert data["pagination"]["next_cursor"] is not None
        assert data["pagination"]["total_count"] == 5
        assert data["pagination"]["limit"] == 1
        assert "next" in data["_links"]

    @pytest.mark.asyncio
    async def test_list_workflows_with_filters(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows passes filter params to service."""
        mock_service.list_workflows = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/workflows?status=deprecated&owner=agent-x&tag=ml&limit=5"
            )

        assert response.status_code == 200
        mock_service.list_workflows.assert_called_once_with(
            status="deprecated",
            owner="agent-x",
            tag="ml",
            limit=5,
            cursor=None,
        )


# ---------------------------------------------------------------------------
# GET /workflows/{workflow_id}
# ---------------------------------------------------------------------------


class TestGetWorkflow:
    @pytest.mark.asyncio
    async def test_get_workflow_success(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows/{id} returns workflow details with _links."""
        mock_wf = _make_workflow()
        mock_service.get_workflow_with_executor_status = AsyncMock(return_value=(mock_wf, "active"))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "wf-test"
        assert data["name"] == "Test Workflow"
        assert data["executor_status"] == "active"
        assert data["_links"]["self"]["href"] == "/workflows/wf-test"
        assert data["_links"]["tasks"]["href"] == "/workflows/wf-test/tasks"
        assert data["_links"]["run"]["href"] == "/workflows/wf-test/run"
        assert data["_links"]["run"]["method"] == "POST"

    @pytest.mark.asyncio
    async def test_get_workflow_not_found(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows/{id} for nonexistent workflow returns 404."""
        from fleet_api.errors import ErrorCode, NotFoundError

        mock_service.get_workflow_with_executor_status = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-ghost")

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_get_workflow_includes_executor_status_null(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows/{id} shows executor_status=null when agent deleted."""
        mock_wf = _make_workflow()
        mock_service.get_workflow_with_executor_status = AsyncMock(return_value=(mock_wf, None))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        assert response.status_code == 200
        data = response.json()
        assert data["executor_status"] is None


# ---------------------------------------------------------------------------
# PUT /workflows/{workflow_id}
# ---------------------------------------------------------------------------


class TestUpdateWorkflow:
    @pytest.mark.asyncio
    async def test_update_workflow_success(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """PUT /workflows/{id} with valid data returns updated workflow."""
        mock_wf = _make_workflow(
            name="Updated Name",
            description="Updated description",
        )
        mock_wf.updated_at = datetime(2026, 3, 7, 13, 0, 0, tzinfo=UTC)
        mock_service.update_workflow = AsyncMock(return_value=mock_wf)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/workflows/wf-test",
                json={
                    "name": "Updated Name",
                    "description": "Updated description",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        assert data["description"] == "Updated description"
        assert data["updated_at"] is not None

    @pytest.mark.asyncio
    async def test_update_workflow_not_owner(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """PUT /workflows/{id} by non-owner returns 403."""
        from fleet_api.errors import AuthError, ErrorCode

        mock_service.update_workflow = AsyncMock(
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the workflow owner can update this workflow.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/workflows/wf-test",
                json={"name": "Hijack"},
            )

        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "NOT_AUTHORIZED"

    @pytest.mark.asyncio
    async def test_update_workflow_not_found(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """PUT /workflows/{id} for nonexistent workflow returns 404."""
        from fleet_api.errors import ErrorCode, NotFoundError

        mock_service.update_workflow = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/workflows/wf-ghost",
                json={"name": "Phantom"},
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_update_workflow_status_deprecated(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """PUT /workflows/{id} can set status to deprecated."""
        mock_wf = _make_workflow(status=WorkflowStatus.DEPRECATED)
        mock_wf.updated_at = datetime(2026, 3, 7, 13, 0, 0, tzinfo=UTC)
        mock_service.update_workflow = AsyncMock(return_value=mock_wf)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.put(
                "/workflows/wf-test",
                json={"status": "deprecated"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deprecated"


# ---------------------------------------------------------------------------
# Service unit tests
# ---------------------------------------------------------------------------


class TestWorkflowServiceValidation:
    """Test the service-level validation logic directly."""

    def test_validate_workflow_id_valid(self) -> None:
        """Valid IDs pass validation."""
        from fleet_api.workflows.service import validate_workflow_id

        validate_workflow_id("wf-code-review")
        validate_workflow_id("my-workflow")
        validate_workflow_id("a")
        validate_workflow_id("A123-test")

    def test_validate_workflow_id_empty(self) -> None:
        """Empty ID raises InputValidationError."""
        from fleet_api.errors import InputValidationError
        from fleet_api.workflows.service import validate_workflow_id

        with pytest.raises(InputValidationError):
            validate_workflow_id("")

    def test_validate_workflow_id_invalid_chars(self) -> None:
        """ID with invalid characters raises InputValidationError."""
        from fleet_api.errors import InputValidationError
        from fleet_api.workflows.service import validate_workflow_id

        with pytest.raises(InputValidationError):
            validate_workflow_id("wf_with_underscores")

        with pytest.raises(InputValidationError):
            validate_workflow_id("wf with spaces")

        with pytest.raises(InputValidationError):
            validate_workflow_id("-starts-with-hyphen")

    def test_validate_workflow_id_too_long(self) -> None:
        """ID over 128 characters raises InputValidationError."""
        from fleet_api.errors import InputValidationError
        from fleet_api.workflows.service import validate_workflow_id

        with pytest.raises(InputValidationError):
            validate_workflow_id("a" * 129)

    def test_validate_json_schema_field_valid(self) -> None:
        """Valid JSON schema objects pass validation."""
        from fleet_api.workflows.service import validate_json_schema_field

        validate_json_schema_field(None, "input_schema")
        validate_json_schema_field(
            {"type": "object", "properties": {}}, "input_schema"
        )

    def test_validate_json_schema_field_missing_type(self) -> None:
        """Schema without 'type' key raises InputValidationError."""
        from fleet_api.errors import InputValidationError
        from fleet_api.workflows.service import validate_json_schema_field

        with pytest.raises(InputValidationError):
            validate_json_schema_field({"properties": {}}, "input_schema")

    def test_encode_decode_cursor(self) -> None:
        """Cursor encode/decode round-trips correctly."""
        from fleet_api.workflows.service import decode_cursor, encode_cursor

        cursor = encode_cursor("wf-alpha")
        assert decode_cursor(cursor) == "wf-alpha"

    def test_decode_invalid_cursor(self) -> None:
        """Invalid cursor raises InputValidationError."""
        from fleet_api.errors import InputValidationError
        from fleet_api.workflows.service import decode_cursor

        with pytest.raises(InputValidationError):
            decode_cursor("not-valid-base64!!!")


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


class TestResponseStructure:
    @pytest.mark.asyncio
    async def test_create_response_has_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows response includes HATEOAS _links."""
        mock_wf = _make_workflow()
        mock_service.create_workflow = AsyncMock(return_value=mock_wf)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows",
                json={"id": "wf-test", "name": "Test Workflow", "result_retention_days": 30},
            )

        data = response.json()
        links = data["_links"]
        assert "self" in links
        assert "run" in links
        assert "tasks" in links
        assert "update" in links
        assert "owner" in links

    @pytest.mark.asyncio
    async def test_list_response_has_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET /workflows response includes _links."""
        mock_service.list_workflows = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        data = response.json()
        assert "_links" in data
        assert "self" in data["_links"]
        assert "register" in data["_links"]

    @pytest.mark.asyncio
    async def test_create_response_has_executor_status_null(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows response includes executor_status (null for create)."""
        mock_wf = _make_workflow()
        mock_service.create_workflow = AsyncMock(return_value=mock_wf)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows",
                json={"id": "wf-test", "name": "Test Workflow", "result_retention_days": 30},
            )

        data = response.json()
        assert "executor_status" in data
        assert data["executor_status"] is None


# ---------------------------------------------------------------------------
# Executor status annotation tests (Issue #31 Gap 2)
# ---------------------------------------------------------------------------


class TestExecutorStatus:
    """Verify that executor_status is returned correctly in workflow responses.

    executor_status reflects the owning agent's current status (active,
    registered, unreachable, suspended) or null when the agent no longer
    exists.  Workflows are NEVER filtered -- only annotated.
    """

    @pytest.mark.asyncio
    async def test_list_workflows_executor_status_active(self) -> None:
        """GET /workflows returns executor_status='active' for active agents."""
        mock_svc = MagicMock()
        wf = _make_workflow(workflow_id="wf-1")
        mock_svc.list_workflows = AsyncMock(
            return_value=([(wf, "active")], None, False, 1)
        )
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["executor_status"] == "active"

    @pytest.mark.asyncio
    async def test_list_workflows_executor_status_unreachable(self) -> None:
        """GET /workflows returns executor_status='unreachable' -- workflow NOT filtered."""
        mock_svc = MagicMock()
        wf = _make_workflow(workflow_id="wf-2")
        mock_svc.list_workflows = AsyncMock(
            return_value=([(wf, "unreachable")], None, False, 1)
        )
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["executor_status"] == "unreachable"
        assert data["data"][0]["id"] == "wf-2"

    @pytest.mark.asyncio
    async def test_list_workflows_executor_status_suspended(self) -> None:
        """GET /workflows returns executor_status='suspended' -- workflow NOT filtered."""
        mock_svc = MagicMock()
        wf = _make_workflow(workflow_id="wf-3")
        mock_svc.list_workflows = AsyncMock(
            return_value=([(wf, "suspended")], None, False, 1)
        )
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["executor_status"] == "suspended"

    @pytest.mark.asyncio
    async def test_list_workflows_executor_status_null_when_agent_deleted(self) -> None:
        """GET /workflows returns executor_status=null when owner agent is deleted."""
        mock_svc = MagicMock()
        wf = _make_workflow(workflow_id="wf-orphan")
        mock_svc.list_workflows = AsyncMock(
            return_value=([(wf, None)], None, False, 1)
        )
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["executor_status"] is None

    @pytest.mark.asyncio
    async def test_list_workflows_mixed_executor_statuses(self) -> None:
        """GET /workflows can return different executor_status values per workflow."""
        mock_svc = MagicMock()
        wf1 = _make_workflow(workflow_id="wf-a", owner="agent-1")
        wf2 = _make_workflow(workflow_id="wf-b", owner="agent-2")
        wf3 = _make_workflow(workflow_id="wf-c", owner="agent-3")
        mock_svc.list_workflows = AsyncMock(
            return_value=(
                [(wf1, "active"), (wf2, "unreachable"), (wf3, None)],
                None,
                False,
                3,
            )
        )
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 3
        assert data["data"][0]["executor_status"] == "active"
        assert data["data"][1]["executor_status"] == "unreachable"
        assert data["data"][2]["executor_status"] is None

    @pytest.mark.asyncio
    async def test_get_workflow_executor_status_active(self) -> None:
        """GET /workflows/{id} returns executor_status='active'."""
        mock_svc = MagicMock()
        wf = _make_workflow()
        mock_svc.get_workflow_with_executor_status = AsyncMock(return_value=(wf, "active"))
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        assert response.status_code == 200
        data = response.json()
        assert data["executor_status"] == "active"

    @pytest.mark.asyncio
    async def test_get_workflow_executor_status_unreachable(self) -> None:
        """GET /workflows/{id} returns executor_status='unreachable'."""
        mock_svc = MagicMock()
        wf = _make_workflow()
        mock_svc.get_workflow_with_executor_status = AsyncMock(return_value=(wf, "unreachable"))
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        assert response.status_code == 200
        data = response.json()
        assert data["executor_status"] == "unreachable"

    @pytest.mark.asyncio
    async def test_get_workflow_executor_status_registered(self) -> None:
        """GET /workflows/{id} returns executor_status='registered'."""
        mock_svc = MagicMock()
        wf = _make_workflow()
        mock_svc.get_workflow_with_executor_status = AsyncMock(return_value=(wf, "registered"))
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        assert response.status_code == 200
        data = response.json()
        assert data["executor_status"] == "registered"

    @pytest.mark.asyncio
    async def test_get_workflow_executor_status_null(self) -> None:
        """GET /workflows/{id} returns executor_status=null when agent deleted."""
        mock_svc = MagicMock()
        wf = _make_workflow()
        mock_svc.get_workflow_with_executor_status = AsyncMock(return_value=(wf, None))
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        assert response.status_code == 200
        data = response.json()
        assert data["executor_status"] is None

    @pytest.mark.asyncio
    async def test_unreachable_workflows_not_filtered(self) -> None:
        """Workflows owned by unreachable agents are returned, not filtered out."""
        mock_svc = MagicMock()
        wf_active = _make_workflow(workflow_id="wf-healthy", owner="agent-ok")
        wf_unreachable = _make_workflow(workflow_id="wf-stale", owner="agent-down")
        mock_svc.list_workflows = AsyncMock(
            return_value=(
                [(wf_active, "active"), (wf_unreachable, "unreachable")],
                None,
                False,
                2,
            )
        )
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows")

        assert response.status_code == 200
        data = response.json()
        ids = [item["id"] for item in data["data"]]
        assert "wf-healthy" in ids
        assert "wf-stale" in ids
        assert data["pagination"]["total_count"] == 2

    @pytest.mark.asyncio
    async def test_executor_status_positioned_after_status(self) -> None:
        """executor_status appears in the response dict (position check)."""
        mock_svc = MagicMock()
        wf = _make_workflow()
        mock_svc.get_workflow_with_executor_status = AsyncMock(return_value=(wf, "active"))
        app = _create_test_app(mock_svc)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-test")

        data = response.json()
        keys = list(data.keys())
        assert "executor_status" in keys
        assert "status" in keys
        # executor_status should come after status in response
        assert keys.index("executor_status") > keys.index("status")
