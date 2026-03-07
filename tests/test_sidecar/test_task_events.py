"""Tests for POST /tasks/{task_id}/events (sidecar event endpoint).

Covers:
  - Status event: accepted -> running transition
  - Status event: running -> completed (via completed event_type)
  - Completed event: stores result, sets completed_at
  - Failed event: transitions to failed, stores error details
  - Progress event: stores event, no status change
  - Log event: stores event, no status change
  - Heartbeat event: stores event
  - Sequence validation: out-of-order -> 422
  - Authorization: non-executor agent -> 403
  - Task not found -> 404
  - Invalid event_type -> 422
  - Invalid status transition -> 409
  - Completed event sets task.result
  - Unauthenticated -> 401
  - Response format with HATEOAS links
  - Started_at set when transitioning to running
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import (
    AuthError,
    ErrorCode,
    InputValidationError,
    NotFoundError,
    StateError,
)
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXECUTOR_AGENT_ID = "executor-agent-001"
OTHER_AGENT_ID = "other-agent-002"
TASK_ID = "task-a1b2c3d4"
WORKFLOW_ID = "wf-code-review"
CREATED_AT = datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _make_event(
    event_id: int = 42,
    task_id: str = TASK_ID,
    event_type: str = "status",
    sequence: int = 1,
    data: dict[str, Any] | None = None,
    created_at: datetime = CREATED_AT,
) -> MagicMock:
    """Create a mock TaskEvent."""
    event = MagicMock(spec=TaskEvent)
    event.id = event_id
    event.task_id = task_id
    event.event_type = event_type
    event.sequence = sequence
    event.data = data or {}
    event.created_at = created_at
    return event


def _make_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    executor_agent_id: str = EXECUTOR_AGENT_ID,
    status: TaskStatus = TaskStatus.ACCEPTED,
) -> MagicMock:
    """Create a mock Task."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.executor_agent_id = executor_agent_id
    task.status = status
    task.principal_agent_id = "caller-agent"
    task.priority = TaskPriority.NORMAL
    task.input = {"pr_url": "https://github.com/..."}
    task.result = None
    task.created_at = CREATED_AT
    task.started_at = None
    task.completed_at = None
    task.metadata_ = None
    return task


def _create_test_app(agent_id: str = EXECUTOR_AGENT_ID) -> Any:
    """Create a test app with auth overrides."""
    app = create_app()

    async def mock_auth() -> AuthenticatedAgent:
        mock_key = MagicMock(spec=Ed25519PublicKey)
        return AuthenticatedAgent(agent_id=agent_id, public_key=mock_key)

    app.dependency_overrides[require_auth] = mock_auth
    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    return app


def _create_unauthenticated_app() -> Any:
    """Create a test app without auth overrides."""
    return create_app()


# ---------------------------------------------------------------------------
# Status events
# ---------------------------------------------------------------------------


class TestStatusEvent:
    """Status event transitions task state."""

    @pytest.mark.asyncio
    async def test_accepted_to_running(self) -> None:
        """Status event transitions accepted -> running."""
        app = _create_test_app()
        mock_event = _make_event(event_type="status", sequence=1)
        mock_task = _make_task(status=TaskStatus.RUNNING)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ) as mock_process:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "status",
                        "data": {"status": "running", "message": "Starting..."},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 201
        mock_process.assert_called_once()
        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["task_id"] == TASK_ID
        assert call_kwargs["event_type"] == "status"
        assert call_kwargs["sequence"] == 1
        assert call_kwargs["executor_agent_id"] == EXECUTOR_AGENT_ID


# ---------------------------------------------------------------------------
# Completed event
# ---------------------------------------------------------------------------


class TestCompletedEvent:
    """Completed event stores result and transitions to completed."""

    @pytest.mark.asyncio
    async def test_completed_stores_result(self) -> None:
        """Completed event returns 201 with event details."""
        app = _create_test_app()
        mock_event = _make_event(event_type="completed", sequence=3)
        mock_task = _make_task(status=TaskStatus.COMPLETED)
        mock_task.result = {"summary": "All checks passed"}

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "completed",
                        "data": {
                            "result": {"summary": "All checks passed"},
                            "quality": {"input_valid": True},
                        },
                        "sequence": 3,
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "completed"
        assert data["task_id"] == TASK_ID

    @pytest.mark.asyncio
    async def test_completed_event_sets_task_result(self) -> None:
        """Completed event passes result data to service."""
        app = _create_test_app()
        mock_event = _make_event(event_type="completed", sequence=2)
        mock_task = _make_task(status=TaskStatus.COMPLETED)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ) as mock_process:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "completed",
                        "data": {"result": {"score": 95}},
                        "sequence": 2,
                    },
                )

        call_kwargs = mock_process.call_args.kwargs
        assert call_kwargs["data"]["result"] == {"score": 95}


# ---------------------------------------------------------------------------
# Failed event
# ---------------------------------------------------------------------------


class TestFailedEvent:
    """Failed event transitions to failed and stores error."""

    @pytest.mark.asyncio
    async def test_failed_event(self) -> None:
        """Failed event returns 201."""
        app = _create_test_app()
        mock_event = _make_event(event_type="failed", sequence=2)
        mock_task = _make_task(status=TaskStatus.FAILED)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "failed",
                        "data": {"error_code": "TIMEOUT", "message": "Timed out"},
                        "sequence": 2,
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "failed"


# ---------------------------------------------------------------------------
# Progress event
# ---------------------------------------------------------------------------


class TestProgressEvent:
    """Progress event stores progress, no status change."""

    @pytest.mark.asyncio
    async def test_progress_event(self) -> None:
        """Progress event returns 201 without changing task status."""
        app = _create_test_app()
        mock_event = _make_event(event_type="progress", sequence=2)
        mock_task = _make_task(status=TaskStatus.RUNNING)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "progress",
                        "data": {"progress": 50, "message": "Halfway done"},
                        "sequence": 2,
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "progress"


# ---------------------------------------------------------------------------
# Log event
# ---------------------------------------------------------------------------


class TestLogEvent:
    """Log event stores event, no status change."""

    @pytest.mark.asyncio
    async def test_log_event(self) -> None:
        """Log event returns 201."""
        app = _create_test_app()
        mock_event = _make_event(event_type="log", sequence=2)
        mock_task = _make_task(status=TaskStatus.RUNNING)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "log",
                        "data": {"level": "info", "message": "Processing file 3 of 10"},
                        "sequence": 2,
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "log"


# ---------------------------------------------------------------------------
# Heartbeat event
# ---------------------------------------------------------------------------


class TestHeartbeatEvent:
    """Heartbeat event records liveness."""

    @pytest.mark.asyncio
    async def test_heartbeat_event(self) -> None:
        """Heartbeat event returns 201."""
        app = _create_test_app()
        mock_event = _make_event(event_type="heartbeat", sequence=5)
        mock_task = _make_task(status=TaskStatus.RUNNING)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "heartbeat",
                        "data": {},
                        "sequence": 5,
                    },
                )

        assert response.status_code == 201
        data = response.json()
        assert data["event_type"] == "heartbeat"


# ---------------------------------------------------------------------------
# Sequence validation
# ---------------------------------------------------------------------------


class TestSequenceValidation:
    """Out-of-order sequence numbers are rejected."""

    @pytest.mark.asyncio
    async def test_out_of_order_sequence_returns_422(self) -> None:
        """Sequence <= last sequence returns 422 INVALID_INPUT."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            side_effect=InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message=(
                    "Sequence 1 is not greater than the last sequence 5"
                    " for task 'task-a1b2c3d4'."
                ),
                suggestion="Use a sequence number greater than the previous event.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "heartbeat",
                        "data": {},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestEventAuthorization:
    """Only the task executor can post events."""

    @pytest.mark.asyncio
    async def test_non_executor_returns_403(self) -> None:
        """Non-executor agent gets 403 NOT_AUTHORIZED."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message=(
                    f"Agent '{OTHER_AGENT_ID}' is not the executor of task '{TASK_ID}'. "
                    f"Only the assigned executor can post events."
                ),
                suggestion="Authenticate as the task's executor agent.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "heartbeat",
                        "data": {},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "NOT_AUTHORIZED"

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self) -> None:
        """Missing auth returns 401."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/tasks/{TASK_ID}/events",
                json={
                    "event_type": "heartbeat",
                    "data": {},
                    "sequence": 1,
                },
            )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Task not found
# ---------------------------------------------------------------------------


class TestEventTaskNotFound:
    """Non-existent task returns 404."""

    @pytest.mark.asyncio
    async def test_task_not_found_returns_404(self) -> None:
        """Non-existent task_id returns 404 TASK_NOT_FOUND."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message="Task 'task-ghost' not found.",
                suggestion="Check the task ID.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/tasks/task-ghost/events",
                    json={
                        "event_type": "heartbeat",
                        "data": {},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Invalid event type
# ---------------------------------------------------------------------------


class TestInvalidEventType:
    """Invalid event_type returns 422."""

    @pytest.mark.asyncio
    async def test_invalid_event_type_returns_422(self) -> None:
        """Unknown event_type returns 422 INVALID_INPUT."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            side_effect=InputValidationError(
                code=ErrorCode.INVALID_INPUT,
                message="Invalid event_type 'banana'.",
                suggestion="Use one of: completed, failed, heartbeat, log, progress, status.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "banana",
                        "data": {},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Invalid status transition
# ---------------------------------------------------------------------------


class TestInvalidStatusTransition:
    """Invalid state transition returns 409."""

    @pytest.mark.asyncio
    async def test_invalid_transition_returns_409(self) -> None:
        """Transitioning completed -> running returns 409."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message="Cannot transition task 'task-a1b2c3d4' from 'completed' to 'running'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "status",
                        "data": {"status": "running"},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "INVALID_STATE_TRANSITION"


# ---------------------------------------------------------------------------
# Response format
# ---------------------------------------------------------------------------


class TestEventResponseFormat:
    """Response contains event_id, task_id, event_type, sequence, created_at, _links."""

    @pytest.mark.asyncio
    async def test_response_fields(self) -> None:
        """Response contains all expected fields."""
        app = _create_test_app()
        mock_event = _make_event(
            event_id=42,
            event_type="status",
            sequence=1,
        )
        mock_task = _make_task(status=TaskStatus.RUNNING)

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "status",
                        "data": {"status": "running"},
                        "sequence": 1,
                    },
                )

        assert response.status_code == 201
        data = response.json()

        expected_fields = {
            "received", "event_id", "task_id", "event_type",
            "sequence", "created_at", "_links",
        }
        assert set(data.keys()) == expected_fields

        assert data["received"] is True
        assert data["event_id"] == 42
        assert data["task_id"] == TASK_ID
        assert data["event_type"] == "status"
        assert data["sequence"] == 1
        assert data["created_at"] is not None

    @pytest.mark.asyncio
    async def test_links_contain_task(self) -> None:
        """_links contains task link with correct href."""
        app = _create_test_app()
        mock_event = _make_event()
        mock_task = _make_task()

        with patch(
            "fleet_api.tasks.routes.process_sidecar_event",
            new_callable=AsyncMock,
            return_value=(mock_event, mock_task),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/tasks/{TASK_ID}/events",
                    json={
                        "event_type": "status",
                        "data": {"status": "running"},
                        "sequence": 1,
                    },
                )

        data = response.json()
        assert data["_links"]["task"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}"
        assert data["_links"]["events"]["href"] == f"/tasks/{TASK_ID}/events"
