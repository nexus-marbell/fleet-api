"""Tests for POST /workflows/{workflow_id}/run — task dispatch.

Uses FastAPI dependency overrides for auth and task service so tests
have no real database dependency. Tests cover:
  - Successful task creation (202)
  - Input validation failure (422)
  - Workflow not found (404)
  - Idempotency replay (same key + same input -> 200)
  - Idempotency mismatch (same key + different input -> 422)
  - Priority validation
  - Auth required
  - Task ID format
  - HATEOAS links ({"href": "..."} format per RFC)
  - Response field names match RFC spec (caller, executor)
  - Input field included in response
  - Executor override via request body
  - Metadata passthrough
  - Optional fields default handling
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import (
    ErrorCode,
    InfrastructureError,
    InputValidationError,
    NotFoundError,
)
from fleet_api.middleware.auth import (
    AuthenticatedAgent,
    get_agent_lookup,
    require_auth,
)
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus
from fleet_api.tasks.routes import get_task_service
from fleet_api.workflows.models import Workflow, WorkflowStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
WORKFLOW_ID = "wf-cellular-automaton"


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------


def _make_workflow(
    workflow_id: str = WORKFLOW_ID,
    owner: str = "grok-pi-dev",
    name: str = "Cellular Automaton",
    input_schema: dict[str, Any] | None = None,
    estimated_duration_seconds: int | None = 15,
    timeout_seconds: int | None = 300,
    status: WorkflowStatus = WorkflowStatus.ACTIVE,
) -> MagicMock:
    """Create a mock Workflow object."""
    wf = MagicMock(spec=Workflow)
    wf.id = workflow_id
    wf.name = name
    wf.owner_agent_id = owner
    wf.input_schema = input_schema
    wf.estimated_duration_seconds = estimated_duration_seconds
    wf.timeout_seconds = timeout_seconds
    wf.status = status
    return wf


def _make_task(
    task_id: str = "task-a1b2c3d4",
    workflow_id: str = WORKFLOW_ID,
    caller: str = AGENT_ID,
    executor: str = "grok-pi-dev",
    status: TaskStatus = TaskStatus.ACCEPTED,
    input_data: dict[str, Any] | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
    timeout_seconds: int | None = 300,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Task object."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = caller
    task.executor_agent_id = executor
    task.status = status
    task.input = input_data or {"rule": 30}
    task.priority = priority
    task.timeout_seconds = timeout_seconds
    task.idempotency_key = idempotency_key
    task.created_at = created_at or datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
    return task


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(
        self, agent_id: str
    ) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _create_test_app(
    mock_service: MagicMock,
    agent_id: str = AGENT_ID,
    auth_override: bool = True,
) -> Any:
    """Create a test app with auth and service overrides."""
    app = create_app()

    if auth_override:

        async def mock_auth() -> AuthenticatedAgent:
            mock_key = MagicMock(spec=Ed25519PublicKey)
            return AuthenticatedAgent(
                agent_id=agent_id, public_key=mock_key
            )

        app.dependency_overrides[require_auth] = mock_auth

    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    app.dependency_overrides[get_task_service] = lambda: mock_service
    return app


def _standard_response(
    task_id: str = "task-a1b2c3d4",
    workflow_id: str = WORKFLOW_ID,
    caller: str = AGENT_ID,
    executor: str = "grok-pi-dev",
    status: str = "accepted",
    input_data: dict[str, Any] | None = None,
    priority: str = "normal",
    timeout_seconds: int | None = 300,
    estimated_duration_seconds: int | None = 15,
    idempotency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard response dict for mock_service.build_task_response."""
    base = f"/workflows/{workflow_id}/tasks/{task_id}"
    resp: dict[str, Any] = {
        "task_id": task_id,
        "workflow_id": workflow_id,
        "status": status,
        "caller": caller,
        "executor": executor,
        "input": input_data or {"rule": 30},
        "priority": priority,
        "timeout_seconds": timeout_seconds,
        "created_at": "2026-03-07T14:30:00+00:00",
        "estimated_duration_seconds": estimated_duration_seconds,
        "_links": {
            "self": {"href": base},
            "stream": {"href": f"{base}/stream"},
            "cancel": {"method": "POST", "href": f"{base}/cancel"},
            "pause": {"method": "POST", "href": f"{base}/pause"},
            "context": {"method": "POST", "href": f"{base}/context"},
            "workflow": {"href": f"/workflows/{workflow_id}"},
        },
    }
    if idempotency is not None:
        resp["idempotency"] = idempotency
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_service() -> MagicMock:
    """Create a mock TaskService."""
    return MagicMock()


@pytest.fixture
def mock_workflow() -> MagicMock:
    """Create a mock Workflow."""
    return _make_workflow()


@pytest.fixture
def app(mock_service: MagicMock) -> Any:
    return _create_test_app(mock_service)


@pytest.fixture
async def client(app: Any) -> AsyncClient:  # type: ignore[misc]
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/run — Success
# ---------------------------------------------------------------------------


class TestTaskRunSuccess:
    @pytest.mark.asyncio
    async def test_create_task_returns_202(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows/{id}/run with valid input returns 202 Accepted."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 202
        data = response.json()
        assert data["task_id"] == "task-a1b2c3d4"
        assert data["workflow_id"] == WORKFLOW_ID
        assert data["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_response_field_names_match_rfc(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Response uses 'caller' and 'executor', not internal column names."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        data = response.json()
        # RFC field names -- NOT internal model column names
        assert "caller" in data
        assert "executor" in data
        assert "principal_agent_id" not in data
        assert "executor_agent_id" not in data

    @pytest.mark.asyncio
    async def test_response_includes_input(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Response includes the input object passed in the request."""
        mock_task = _make_task(input_data={"rule": 42, "steps": 100})
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(
                input_data={"rule": 42, "steps": 100}
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 42, "steps": 100}},
            )

        data = response.json()
        assert "input" in data
        assert data["input"]["rule"] == 42
        assert data["input"]["steps"] == 100

    @pytest.mark.asyncio
    async def test_task_id_format(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Task ID starts with 'task-' prefix."""
        mock_task = _make_task(task_id="task-f7e8d9c0")
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(task_id="task-f7e8d9c0")
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        data = response.json()
        assert data["task_id"].startswith("task-")

    @pytest.mark.asyncio
    async def test_hateoas_links_present(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Response includes correct HATEOAS _links for accepted status."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        data = response.json()
        links = data["_links"]
        assert "self" in links
        assert "stream" in links
        assert "pause" in links
        assert "cancel" in links
        assert "context" in links
        assert "workflow" in links
        # Verify href format (dict with "href" key, not plain strings)
        assert (
            links["self"]["href"]
            == f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
        )
        assert (
            links["stream"]["href"]
            == f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4/stream"
        )
        assert links["workflow"]["href"] == f"/workflows/{WORKFLOW_ID}"
        # Action links include method
        assert links["cancel"]["method"] == "POST"
        assert (
            links["cancel"]["href"]
            == f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4/cancel"
        )

    @pytest.mark.asyncio
    async def test_create_task_with_priority(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Task creation respects priority parameter."""
        mock_task = _make_task(priority=TaskPriority.HIGH)
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(priority="high")
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}, "priority": "high"},
            )

        assert response.status_code == 202
        data = response.json()
        assert data["priority"] == "high"
        # Verify the service was called with the correct priority
        mock_service.create_task.assert_called_once()
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["priority"] == "high"

    @pytest.mark.asyncio
    async def test_create_task_with_executor_override(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Task creation passes executor from request body to service."""
        mock_task = _make_task(executor="custom-executor-agent")
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(
                executor="custom-executor-agent"
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={
                    "input": {"rule": 30},
                    "executor": "custom-executor-agent",
                },
            )

        assert response.status_code == 202
        data = response.json()
        assert data["executor"] == "custom-executor-agent"
        call_kwargs = mock_service.create_task.call_args
        assert (
            call_kwargs.kwargs["executor_agent_id"]
            == "custom-executor-agent"
        )

    @pytest.mark.asyncio
    async def test_create_task_with_metadata(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Task creation passes metadata from request body to service."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={
                    "input": {"rule": 30},
                    "metadata": {"source": "test", "trace_id": "abc123"},
                },
            )

        assert response.status_code == 202
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["metadata"] == {
            "source": "test",
            "trace_id": "abc123",
        }

    @pytest.mark.asyncio
    async def test_default_priority_is_normal(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """When priority is not provided, it defaults to 'normal'."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 202
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["priority"] == "normal"

    @pytest.mark.asyncio
    async def test_optional_fields_default_to_none(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Optional fields (executor, timeout, metadata) default to None."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 202
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["executor_agent_id"] is None
        assert call_kwargs.kwargs["timeout_seconds"] is None
        assert call_kwargs.kwargs["metadata"] is None


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/run — Errors
# ---------------------------------------------------------------------------


class TestTaskRunErrors:
    @pytest.mark.asyncio
    async def test_workflow_not_found(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows/{id}/run for nonexistent workflow returns 404."""
        mock_service.create_task = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
                suggestion="Check the workflow ID.",
                links={"workflows": "/workflows"},
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                "/workflows/wf-ghost/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_input_validation_failure(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows/{id}/run with invalid input returns 422."""
        mock_service.create_task = AsyncMock(
            side_effect=InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "Input validation failed: "
                    "'rule' is a required property"
                ),
                suggestion=(
                    "Check the input against the workflow's input_schema."
                ),
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"wrong_field": "value"}},
            )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_invalid_priority(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows/{id}/run with invalid priority returns 422."""
        mock_service.create_task = AsyncMock(
            side_effect=InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "Invalid priority 'urgent'. "
                    "Must be one of: low, normal, high, critical."
                ),
                suggestion="Use one of: low, normal, high, critical.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}, "priority": "urgent"},
            )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "INVALID_INPUT"

    @pytest.mark.asyncio
    async def test_agent_suspended(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows/{id}/run when executor is suspended returns 503."""
        mock_service.create_task = AsyncMock(
            side_effect=InfrastructureError(
                code=ErrorCode.AGENT_SUSPENDED,
                message=(
                    "Workflow owner agent 'grok-pi-dev' is suspended."
                ),
                suggestion=(
                    "The executor agent is currently unavailable. "
                    "Try again later."
                ),
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 503
        data = response.json()
        assert data["code"] == "AGENT_SUSPENDED"

    @pytest.mark.asyncio
    async def test_missing_input_field(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """POST /workflows/{id}/run without 'input' field returns 422."""
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"priority": "normal"},
            )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/run — Auth required
# ---------------------------------------------------------------------------


class TestTaskRunAuth:
    @pytest.mark.asyncio
    async def test_auth_required(self, mock_service: MagicMock) -> None:
        """POST /workflows/{id}/run without auth returns 401."""
        # Create app WITHOUT auth override (uses real require_auth which
        # will fail because no Authorization header is provided)
        app = _create_test_app(mock_service, auth_override=False)

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        # Should get 401 because no auth header
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/run — Idempotency
# ---------------------------------------------------------------------------


class TestTaskRunIdempotency:
    @pytest.mark.asyncio
    async def test_idempotency_replay(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Same Idempotency-Key + same input returns 200 with replayed."""
        mock_task = _make_task(
            idempotency_key="run-ca-rule30-2026-03-07"
        )
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, True)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(
                idempotency={
                    "key": "run-ca-rule30-2026-03-07",
                    "status": "replayed",
                    "expires_at": "2026-03-08T14:30:00+00:00",
                },
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
                headers={
                    "Idempotency-Key": "run-ca-rule30-2026-03-07"
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["idempotency"]["status"] == "replayed"
        assert (
            data["idempotency"]["key"] == "run-ca-rule30-2026-03-07"
        )

    @pytest.mark.asyncio
    async def test_idempotency_mismatch(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Same Idempotency-Key + different input returns 422."""
        mock_service.create_task = AsyncMock(
            side_effect=InputValidationError(
                code=ErrorCode.IDEMPOTENCY_MISMATCH,
                message=(
                    "Idempotency key 'run-ca-rule30-2026-03-07' "
                    "was already used with different input."
                ),
                suggestion=(
                    "Use a new idempotency key for different input."
                ),
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 110}},
                headers={
                    "Idempotency-Key": "run-ca-rule30-2026-03-07"
                },
            )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "IDEMPOTENCY_MISMATCH"

    @pytest.mark.asyncio
    async def test_idempotency_new_key(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """New Idempotency-Key creates task with idempotency block."""
        mock_task = _make_task(idempotency_key="brand-new-key")
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(
                idempotency={
                    "key": "brand-new-key",
                    "status": "created",
                    "expires_at": "2026-03-08T14:30:00+00:00",
                },
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
                headers={"Idempotency-Key": "brand-new-key"},
            )

        assert response.status_code == 202
        data = response.json()
        assert data["idempotency"]["status"] == "created"
        assert data["idempotency"]["key"] == "brand-new-key"
        assert data["idempotency"]["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_no_idempotency_header_no_block(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Without Idempotency-Key header, no idempotency block."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 202
        data = response.json()
        assert "idempotency" not in data

    @pytest.mark.asyncio
    async def test_idempotency_key_from_body(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Idempotency key can be provided in request body."""
        mock_task = _make_task(idempotency_key="body-key")
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(
                idempotency={
                    "key": "body-key",
                    "status": "created",
                    "expires_at": "2026-03-08T14:30:00+00:00",
                },
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={
                    "input": {"rule": 30},
                    "idempotency_key": "body-key",
                },
            )

        assert response.status_code == 202
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["idempotency_key"] == "body-key"

    @pytest.mark.asyncio
    async def test_header_idempotency_key_takes_precedence(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Header Idempotency-Key takes precedence over body."""
        mock_task = _make_task(idempotency_key="header-key")
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(
            return_value=(mock_task, False)
        )
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_standard_response(
                idempotency={
                    "key": "header-key",
                    "status": "created",
                    "expires_at": "2026-03-08T14:30:00+00:00",
                },
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={
                    "input": {"rule": 30},
                    "idempotency_key": "body-key",
                },
                headers={"Idempotency-Key": "header-key"},
            )

        assert response.status_code == 202
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["idempotency_key"] == "header-key"


# ---------------------------------------------------------------------------
# Service unit tests — build_task_links
# ---------------------------------------------------------------------------


class TestBuildTaskLinks:
    def test_accepted_status_links(self) -> None:
        """Accepted status has self, stream, pause, cancel, context, workflow."""
        from fleet_api.tasks.service import build_task_links

        links = build_task_links("task-abc", "wf-test", "accepted")
        assert (
            links["self"]["href"]
            == "/workflows/wf-test/tasks/task-abc"
        )
        assert (
            links["stream"]["href"]
            == "/workflows/wf-test/tasks/task-abc/stream"
        )
        assert links["workflow"]["href"] == "/workflows/wf-test"
        assert links["pause"]["method"] == "POST"
        assert (
            links["pause"]["href"]
            == "/workflows/wf-test/tasks/task-abc/pause"
        )
        assert links["cancel"]["method"] == "POST"
        assert (
            links["cancel"]["href"]
            == "/workflows/wf-test/tasks/task-abc/cancel"
        )
        assert links["context"]["method"] == "POST"
        assert (
            links["context"]["href"]
            == "/workflows/wf-test/tasks/task-abc/context"
        )

    def test_completed_status_links(self) -> None:
        """Completed status has no action links."""
        from fleet_api.tasks.service import build_task_links

        links = build_task_links("task-abc", "wf-test", "completed")
        assert "self" in links
        assert "stream" in links
        assert "workflow" in links
        assert "pause" not in links
        assert "cancel" not in links
        assert "context" not in links

    def test_running_status_links(self) -> None:
        """Running status includes pause, cancel, context."""
        from fleet_api.tasks.service import build_task_links

        links = build_task_links("task-abc", "wf-test", "running")
        assert "pause" in links
        assert "cancel" in links
        assert "context" in links

    def test_paused_status_has_context_only(self) -> None:
        """Paused status has context but not cancel or pause."""
        from fleet_api.tasks.service import build_task_links

        links = build_task_links("task-abc", "wf-test", "paused")
        assert "context" in links
        assert "cancel" not in links
        assert "pause" not in links


# ---------------------------------------------------------------------------
# Service unit tests — validate_input
# ---------------------------------------------------------------------------


class TestValidateInput:
    def test_validate_input_no_schema(self) -> None:
        """No schema means any input is valid."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        svc.validate_input({"anything": "goes"}, None)

    def test_validate_input_valid(self) -> None:
        """Input matching schema passes."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        schema = {
            "type": "object",
            "properties": {"rule": {"type": "integer"}},
            "required": ["rule"],
        }
        svc.validate_input({"rule": 30}, schema)

    def test_validate_input_invalid(self) -> None:
        """Input failing schema raises InputValidationError."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        schema = {
            "type": "object",
            "properties": {"rule": {"type": "integer"}},
            "required": ["rule"],
        }
        with pytest.raises(InputValidationError) as exc_info:
            svc.validate_input({"wrong": "field"}, schema)
        assert exc_info.value.code == ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------------------
# Service unit tests — build_task_response
# ---------------------------------------------------------------------------


class TestBuildTaskResponse:
    def test_response_with_idempotency(self) -> None:
        """build_task_response includes idempotency block when key is present."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task(idempotency_key="my-key")
        wf = _make_workflow()

        resp = svc.build_task_response(
            task, wf, is_replay=False, idempotency_key="my-key"
        )
        assert resp["idempotency"]["key"] == "my-key"
        assert resp["idempotency"]["status"] == "created"
        assert resp["idempotency"]["expires_at"] is not None

    def test_response_replay(self) -> None:
        """build_task_response marks idempotency as replayed."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task(idempotency_key="my-key")
        wf = _make_workflow()

        resp = svc.build_task_response(
            task, wf, is_replay=True, idempotency_key="my-key"
        )
        assert resp["idempotency"]["status"] == "replayed"

    def test_response_without_idempotency(self) -> None:
        """build_task_response omits idempotency block when no key."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task(idempotency_key=None)
        wf = _make_workflow()

        resp = svc.build_task_response(
            task, wf, is_replay=False, idempotency_key=None
        )
        assert "idempotency" not in resp

    def test_response_field_names(self) -> None:
        """build_task_response uses RFC field names: caller, executor."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task()
        wf = _make_workflow()

        resp = svc.build_task_response(
            task, wf, is_replay=False, idempotency_key=None
        )
        assert "caller" in resp
        assert "executor" in resp
        assert "principal_agent_id" not in resp
        assert "executor_agent_id" not in resp
        assert resp["caller"] == AGENT_ID
        assert resp["executor"] == "grok-pi-dev"
        assert resp["estimated_duration_seconds"] == 15

    def test_response_includes_input(self) -> None:
        """build_task_response includes the task input in the response."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task(input_data={"rule": 30})
        wf = _make_workflow()

        resp = svc.build_task_response(
            task, wf, is_replay=False, idempotency_key=None
        )
        assert "input" in resp
        assert resp["input"] == {"rule": 30}

    def test_response_hateoas_href_format(self) -> None:
        """build_task_response _links use {"href": "..."} format."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task()
        wf = _make_workflow()

        resp = svc.build_task_response(
            task, wf, is_replay=False, idempotency_key=None
        )
        links = resp["_links"]
        assert isinstance(links["self"], dict)
        assert "href" in links["self"]
        assert isinstance(links["workflow"], dict)
        assert "href" in links["workflow"]
