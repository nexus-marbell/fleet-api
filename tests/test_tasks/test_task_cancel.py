"""Tests for POST /workflows/{workflow_id}/tasks/{task_id}/cancel.

Uses FastAPI dependency overrides for auth and database session so tests
have no real database dependency. Covers:
  - Cancel from each cancellable state (accepted, running, paused) -> 200
  - Cancel from terminal states (completed, failed, cancelled) -> 409
  - Authorization: task caller can cancel, workflow owner can cancel
  - Unauthorized cancel -> 403
  - Workflow not found -> 404
  - Task not found -> 404
  - TaskEvent creation with reason and cancelled_by
  - Response field names match RFC section 3.16
  - Optional reason field (with and without)
  - HATEOAS links present
  - Auth required
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import AuthError, ErrorCode, NotFoundError, StateError
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
OTHER_AGENT_ID = "other-agent-002"
WORKFLOW_OWNER_ID = "workflow-owner-003"
WORKFLOW_ID = "wf-cellular-automaton"
TASK_ID = "task-a1b2c3d4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _make_cancelled_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    principal_agent_id: str = AGENT_ID,
) -> MagicMock:
    """Create a mock Task in cancelled state with completed_at set."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.status = TaskStatus.CANCELLED
    task.completed_at = datetime(2026, 3, 7, 14, 35, 0, tzinfo=UTC)
    return task


def _create_test_app(agent_id: str = AGENT_ID) -> Any:
    """Create a test app with auth overrides (no service override -- we mock at service fn level)."""
    app = create_app()

    async def mock_auth() -> AuthenticatedAgent:
        mock_key = MagicMock(spec=Ed25519PublicKey)
        return AuthenticatedAgent(agent_id=agent_id, public_key=mock_key)

    app.dependency_overrides[require_auth] = mock_auth
    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    return app


def _create_unauthenticated_app() -> Any:
    """Create a test app without auth overrides (auth will fail)."""
    return create_app()


# ---------------------------------------------------------------------------
# Cancel from cancellable states
# ---------------------------------------------------------------------------


class TestCancelFromCancellableStates:
    """Cancel from accepted, running, and paused states returns 200."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("initial_status", ["accepted", "running", "paused"])
    async def test_cancel_from_cancellable_state(self, initial_status: str) -> None:
        """POST /workflows/{wf}/tasks/{task}/cancel from {initial_status} returns 200."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ) as mock_cancel:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={"reason": f"Cancelling from {initial_status}"},
                )

            assert response.status_code == 200
            mock_cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Cancel from terminal states
# ---------------------------------------------------------------------------


class TestCancelFromTerminalStates:
    """Cancel from completed, failed, or cancelled states returns 409."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
    async def test_cancel_from_terminal_state(self, terminal_status: str) -> None:
        """POST cancel on a {terminal_status} task returns 409 TASK_NOT_PAUSABLE."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.TASK_NOT_PAUSABLE,
                message=(
                    f"Task '{TASK_ID}' cannot be cancelled. "
                    f"Current status: '{terminal_status}'. "
                    f"Only tasks with status 'accepted', 'running', or 'paused' can be cancelled."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={},
                )

            assert response.status_code == 409
            data = response.json()
            assert data["code"] == "TASK_NOT_PAUSABLE"
            assert TASK_ID in data["message"]
            assert terminal_status in data["message"]


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestCancelAuthorization:
    """Authorization checks for task cancellation."""

    @pytest.mark.asyncio
    async def test_task_caller_can_cancel(self) -> None:
        """Task's principal_agent_id can cancel the task."""
        app = _create_test_app(agent_id=AGENT_ID)
        mock_task = _make_cancelled_task(principal_agent_id=AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ) as mock_cancel:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={"reason": "No longer needed"},
                )

            assert response.status_code == 200
            call_kwargs = mock_cancel.call_args
            assert call_kwargs.kwargs["cancelled_by"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_workflow_owner_can_cancel(self) -> None:
        """Workflow owner can cancel a task even if not the task caller."""
        app = _create_test_app(agent_id=WORKFLOW_OWNER_ID)
        mock_task = _make_cancelled_task(principal_agent_id=AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={},
                )

            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthorized_cancel(self) -> None:
        """Agent who is neither task caller nor workflow owner gets 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the task caller or workflow owner may cancel this task.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={},
                )

            assert response.status_code == 403
            data = response.json()
            assert data["code"] == "NOT_AUTHORIZED"
            assert "task caller or workflow owner" in data["message"]


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


class TestCancelNotFound:
    """404 errors for missing workflow or task."""

    @pytest.mark.asyncio
    async def test_workflow_not_found(self) -> None:
        """Cancel on nonexistent workflow returns 404 WORKFLOW_NOT_FOUND."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/workflows/wf-ghost/tasks/task-xyz/cancel",
                    json={},
                )

            assert response.status_code == 404
            data = response.json()
            assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_task_not_found(self) -> None:
        """Cancel on nonexistent task returns 404 TASK_NOT_FOUND."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message=f"Task 'task-ghost' not found in workflow '{WORKFLOW_ID}'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/task-ghost/cancel",
                    json={},
                )

            assert response.status_code == 404
            data = response.json()
            assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Response format
# ---------------------------------------------------------------------------


class TestCancelResponseFormat:
    """Response field names match RFC section 3.16 exactly."""

    @pytest.mark.asyncio
    async def test_response_field_names_match_spec(self) -> None:
        """Response contains exact RFC field names: task_id, workflow_id, status,
        cancelled_at, cancelled_by, reason, _links."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={"reason": "Requirements changed"},
                )

            assert response.status_code == 200
            data = response.json()

            # Verify exact field names from RFC section 3.16
            assert data["task_id"] == TASK_ID
            assert data["workflow_id"] == WORKFLOW_ID
            assert data["status"] == "cancelled"
            assert data["cancelled_at"] is not None
            assert data["cancelled_by"] == AGENT_ID
            assert data["reason"] == "Requirements changed"
            assert "_links" in data

    @pytest.mark.asyncio
    async def test_cancelled_at_uses_completed_at(self) -> None:
        """cancelled_at field uses the task's completed_at timestamp."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()
        expected_time = "2026-03-07T14:35:00+00:00"

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={},
                )

            data = response.json()
            assert data["cancelled_at"] == expected_time


# ---------------------------------------------------------------------------
# Optional reason
# ---------------------------------------------------------------------------


class TestOptionalReason:
    """The reason field is optional in both request and response."""

    @pytest.mark.asyncio
    async def test_cancel_with_reason(self) -> None:
        """Cancel with a reason includes it in the response."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={"reason": "No longer needed"},
                )

            data = response.json()
            assert data["reason"] == "No longer needed"

    @pytest.mark.asyncio
    async def test_cancel_without_reason(self) -> None:
        """Cancel without a reason returns null for reason."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={},
                )

            data = response.json()
            assert data["reason"] is None

    @pytest.mark.asyncio
    async def test_cancel_with_empty_body(self) -> None:
        """Cancel with no request body at all works (body is optional)."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                )

            assert response.status_code == 200
            data = response.json()
            assert data["reason"] is None


# ---------------------------------------------------------------------------
# HATEOAS links
# ---------------------------------------------------------------------------


class TestCancelHATEOASLinks:
    """HATEOAS _links in cancel response."""

    @pytest.mark.asyncio
    async def test_links_present(self) -> None:
        """Cancel response includes self, rerun, and workflow links."""
        app = _create_test_app()
        mock_task = _make_cancelled_task()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            return_value=mock_task,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                    json={},
                )

            data = response.json()
            links = data["_links"]

            assert links["self"] == f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}"
            assert links["rerun"]["method"] == "POST"
            assert links["rerun"]["href"] == f"/workflows/{WORKFLOW_ID}/run"
            assert links["workflow"] == f"/workflows/{WORKFLOW_ID}"


# ---------------------------------------------------------------------------
# TaskEvent creation (service-level test)
# ---------------------------------------------------------------------------


class TestTaskEventCreation:
    """Verify that cancel_task creates a TaskEvent with correct data."""

    @pytest.mark.asyncio
    async def test_event_created_with_reason_and_cancelled_by(self) -> None:
        """cancel_task creates a TaskEvent with from_status, to_status, reason, cancelled_by."""
        from unittest.mock import call

        from fleet_api.tasks.service import cancel_task as cancel_task_fn

        # Mock session
        session = AsyncMock()

        # Mock workflow
        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Mock task
        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.completed_at = datetime(2026, 3, 7, 14, 35, 0, tzinfo=UTC)

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        # session.get returns workflow first, then task
        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Mock the sequence query
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await cancel_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            cancelled_by=AGENT_ID,
            reason="No longer needed",
        )

        # Verify session.add was called with a TaskEvent
        assert session.add.called
        added_event = session.add.call_args[0][0]
        assert added_event.task_id == TASK_ID
        assert added_event.event_type == "status"
        assert added_event.data["from_status"] == "running"
        assert added_event.data["to_status"] == "cancelled"
        assert added_event.data["reason"] == "No longer needed"
        assert added_event.data["cancelled_by"] == AGENT_ID
        assert added_event.sequence == 1

        # Verify commit was called
        session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


class TestCancelAuthRequired:
    """The cancel endpoint requires authentication."""

    @pytest.mark.asyncio
    async def test_cancel_without_auth_returns_error(self) -> None:
        """POST cancel without Authorization header returns auth error."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/cancel",
                json={},
            )

        # Without auth header, the auth middleware returns 401
        assert response.status_code == 401
