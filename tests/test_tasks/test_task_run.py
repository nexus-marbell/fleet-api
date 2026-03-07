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
  - HATEOAS links
  - Response field names match RFC spec
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
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus
from fleet_api.tasks.routes import get_task_service
from fleet_api.workflows.models import Workflow, WorkflowStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
WORKFLOW_ID = "wf-cellular-automaton"
TASK_ID = "task-a1b2c3d4"


def _task_base_path(task_id: str = TASK_ID, wf_id: str = WORKFLOW_ID) -> str:
    return f"/workflows/{wf_id}/tasks/{task_id}"


def _mock_links(task_id: str = TASK_ID, wf_id: str = WORKFLOW_ID) -> dict:
    """Build the expected HATEOAS links for test assertions."""
    base = _task_base_path(task_id, wf_id)
    return {
        "self": base,
        "stream": f"{base}/stream",
        "pause": {"method": "POST", "href": f"{base}/pause"},
        "cancel": {"method": "POST", "href": f"{base}/cancel"},
        "context": {"method": "POST", "href": f"{base}/context"},
        "workflow": f"/workflows/{wf_id}",
    }


def _mock_response(
    task_id: str = TASK_ID,
    include_links: bool = True,
    idempotency: dict | None = None,
    priority: str = "normal",
) -> dict:
    """Build a standard mock response dict."""
    resp: dict[str, Any] = {
        "task_id": task_id,
        "workflow_id": WORKFLOW_ID,
        "status": "accepted",
        "caller": AGENT_ID,
        "executor": "grok-pi-dev",
        "priority": priority,
        "timeout_seconds": 300,
        "created_at": "2026-03-07T14:30:00+00:00",
        "estimated_duration_seconds": 15,
        "_links": _mock_links(task_id) if include_links else {},
    }
    if idempotency is not None:
        resp["idempotency"] = idempotency
    return resp


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

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
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
            return AuthenticatedAgent(agent_id=agent_id, public_key=mock_key)

        app.dependency_overrides[require_auth] = mock_auth

    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    app.dependency_overrides[get_task_service] = lambda: mock_service
    return app


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
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 202
        data = response.json()
        assert data["task_id"] == TASK_ID
        assert data["workflow_id"] == WORKFLOW_ID
        assert data["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_response_field_names_match_rfc(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Response uses 'caller' and 'executor', not internal column names."""
        mock_task = _make_task()
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response(include_links=False)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        data = response.json()
        # RFC field names — NOT internal model column names
        assert "caller" in data
        assert "executor" in data
        assert "principal_agent_id" not in data
        assert "executor_agent_id" not in data

    @pytest.mark.asyncio
    async def test_task_id_format(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Task ID starts with 'task-' prefix."""
        alt_id = "task-f7e8d9c0"
        mock_task = _make_task(task_id=alt_id)
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response(task_id=alt_id, include_links=False)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response()
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        # Verify paths are correct
        base = _task_base_path()
        assert links["self"] == base
        assert links["stream"] == f"{base}/stream"
        assert links["workflow"] == f"/workflows/{WORKFLOW_ID}"

    @pytest.mark.asyncio
    async def test_create_task_with_priority(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Task creation respects priority parameter."""
        mock_task = _make_task(priority=TaskPriority.HIGH)
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response(
                include_links=False, priority="high"
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
                message="Input validation failed: 'rule' is a required property",
                suggestion="Check the input against the workflow's input_schema.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
                message="Invalid priority 'urgent'. Must be one of: low, normal, high, critical.",
                suggestion="Use one of: low, normal, high, critical.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
                message="Workflow owner agent 'grok-pi-dev' is suspended.",
                suggestion="The executor agent is currently unavailable. Try again later.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 503
        data = response.json()
        assert data["code"] == "AGENT_SUSPENDED"


# ---------------------------------------------------------------------------
# POST /workflows/{workflow_id}/run — Auth required
# ---------------------------------------------------------------------------


class TestTaskRunAuth:
    @pytest.mark.asyncio
    async def test_auth_required(self, mock_service: MagicMock) -> None:
        """POST /workflows/{id}/run without auth returns 401."""
        # Create app WITHOUT auth override (uses real require_auth which will
        # fail because no Authorization header is provided)
        app = _create_test_app(mock_service, auth_override=False)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        """Same Idempotency-Key + same input returns 200 with replayed status."""
        idem_key = "run-ca-rule30-2026-03-07"
        mock_task = _make_task(idempotency_key=idem_key)
        mock_wf = _make_workflow()
        mock_service.create_task = AsyncMock(return_value=(mock_task, True))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response(
                include_links=False,
                idempotency={
                    "key": idem_key,
                    "status": "replayed",
                    "expires_at": "2026-03-08T14:30:00+00:00",
                },
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
                headers={"Idempotency-Key": idem_key},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["idempotency"]["status"] == "replayed"
        assert data["idempotency"]["key"] == idem_key

    @pytest.mark.asyncio
    async def test_idempotency_mismatch(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Same Idempotency-Key + different input returns 422."""
        idem_key = "run-ca-rule30-2026-03-07"
        mock_service.create_task = AsyncMock(
            side_effect=InputValidationError(
                code=ErrorCode.IDEMPOTENCY_MISMATCH,
                message=(
                    f"Idempotency key '{idem_key}' was already "
                    "used with different input."
                ),
                suggestion="Use a new idempotency key.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 110}},
                headers={"Idempotency-Key": idem_key},
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
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response(
                include_links=False,
                idempotency={
                    "key": "brand-new-key",
                    "status": "created",
                    "expires_at": "2026-03-08T14:30:00+00:00",
                },
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
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
        mock_service.create_task = AsyncMock(return_value=(mock_task, False))
        mock_service.get_workflow_or_404 = AsyncMock(return_value=mock_wf)
        mock_service.build_task_response = MagicMock(
            return_value=_mock_response(include_links=False)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/run",
                json={"input": {"rule": 30}},
            )

        assert response.status_code == 202
        data = response.json()
        assert "idempotency" not in data


# ---------------------------------------------------------------------------
# Service unit tests — build_task_links
# ---------------------------------------------------------------------------


class TestBuildTaskLinks:
    def test_accepted_status_links(self) -> None:
        """Accepted status includes self, stream, pause, cancel, context, workflow."""
        from fleet_api.tasks.service import build_task_links

        links = build_task_links("task-abc", "wf-test", "accepted")
        assert links["self"] == "/workflows/wf-test/tasks/task-abc"
        assert links["stream"] == "/workflows/wf-test/tasks/task-abc/stream"
        assert links["workflow"] == "/workflows/wf-test"
        assert links["pause"]["method"] == "POST"
        assert links["pause"]["href"] == "/workflows/wf-test/tasks/task-abc/pause"
        assert links["cancel"]["method"] == "POST"
        assert links["cancel"]["href"] == "/workflows/wf-test/tasks/task-abc/cancel"
        assert links["context"]["method"] == "POST"
        assert links["context"]["href"] == "/workflows/wf-test/tasks/task-abc/context"

    def test_completed_status_links(self) -> None:
        """Completed status has no action links (pause, cancel, context)."""
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


# ---------------------------------------------------------------------------
# Service unit tests — validate_input
# ---------------------------------------------------------------------------


class TestValidateInput:
    def test_validate_input_no_schema(self) -> None:
        """No schema means any input is valid."""
        from fleet_api.tasks.service import TaskService

        # We can call validate_input directly without a session
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

        resp = svc.build_task_response(task, wf, is_replay=False, idempotency_key="my-key")
        assert resp["idempotency"]["key"] == "my-key"
        assert resp["idempotency"]["status"] == "created"
        assert resp["idempotency"]["expires_at"] is not None

    def test_response_replay(self) -> None:
        """build_task_response marks idempotency as replayed."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task(idempotency_key="my-key")
        wf = _make_workflow()

        resp = svc.build_task_response(task, wf, is_replay=True, idempotency_key="my-key")
        assert resp["idempotency"]["status"] == "replayed"

    def test_response_without_idempotency(self) -> None:
        """build_task_response omits idempotency block when no key."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task(idempotency_key=None)
        wf = _make_workflow()

        resp = svc.build_task_response(task, wf, is_replay=False, idempotency_key=None)
        assert "idempotency" not in resp

    def test_response_field_names(self) -> None:
        """build_task_response uses RFC field names: caller, executor."""
        from fleet_api.tasks.service import TaskService

        svc = TaskService.__new__(TaskService)
        task = _make_task()
        wf = _make_workflow()

        resp = svc.build_task_response(task, wf, is_replay=False, idempotency_key=None)
        assert "caller" in resp
        assert "executor" in resp
        assert "principal_agent_id" not in resp
        assert "executor_agent_id" not in resp
        assert resp["caller"] == AGENT_ID
        assert resp["executor"] == "grok-pi-dev"
        assert resp["estimated_duration_seconds"] == 15
