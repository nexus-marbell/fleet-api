"""Tests for POST /workflows/{workflow_id}/tasks/{task_id}/cancel.

Uses FastAPI dependency overrides for auth and database session so tests
have no real database dependency. Covers:
  - Cancel from each cancellable state (accepted, running, paused) -> 200
  - Cancel from terminal states (completed, failed, cancelled) -> 409
  - Authorization: task caller can cancel, workflow owner can cancel
  - Unauthorized cancel -> 403
  - Workflow not found -> 404
  - Task not found -> 404
  - Task doesn't belong to workflow -> 404
  - TaskEvent creation with reason and cancelled_by
  - Response field names match RFC (caller, executor)
  - Optional reason field (with and without)
  - HATEOAS links: only self + workflow (no action links)
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
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
OTHER_AGENT_ID = "other-agent-002"
WORKFLOW_OWNER_ID = "workflow-owner-003"
WORKFLOW_ID = "wf-cellular-automaton"
TASK_ID = "task-a1b2c3d4"
CREATED_AT = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 3, 7, 14, 35, 0, tzinfo=UTC)


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
    executor_agent_id: str | None = "executor-agent-xyz",
    task_input: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Task in cancelled state with all RFC fields populated."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = TaskStatus.CANCELLED
    task.input = task_input if task_input is not None else {"prompt": "test"}
    task.result = result
    task.priority = TaskPriority.NORMAL
    task.created_at = CREATED_AT
    task.completed_at = COMPLETED_AT
    return task


def _create_test_app(agent_id: str = AGENT_ID) -> Any:
    """Create a test app with auth overrides."""
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
            data = response.json()
            assert data["status"] == "cancelled"


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

    @pytest.mark.asyncio
    async def test_task_not_in_workflow(self) -> None:
        """Cancel with task that exists but belongs to different workflow returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.cancel_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message="Task 'task-wrong-wf' not found in workflow 'wf-other'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/workflows/wf-other/tasks/task-wrong-wf/cancel",
                    json={},
                )

            assert response.status_code == 404
            data = response.json()
            assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Response format — RFC field names
# ---------------------------------------------------------------------------


class TestCancelResponseFormat:
    """Response field names match RFC exactly: caller, executor (not *_agent_id)."""

    @pytest.mark.asyncio
    async def test_response_contains_all_rfc_fields(self) -> None:
        """Response contains exact RFC fields: task_id, workflow_id, caller,
        executor, status, input, result, priority, created_at, completed_at, _links."""
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

            # All RFC fields present
            expected_fields = {
                "task_id", "workflow_id", "caller", "executor",
                "status", "input", "result", "priority",
                "created_at", "completed_at", "_links",
            }
            assert set(data.keys()) == expected_fields

    @pytest.mark.asyncio
    async def test_caller_is_principal_agent_id(self) -> None:
        """The 'caller' field maps from task.principal_agent_id."""
        app = _create_test_app()
        mock_task = _make_cancelled_task(principal_agent_id="agent-abc")

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
            assert data["caller"] == "agent-abc"

    @pytest.mark.asyncio
    async def test_executor_is_executor_agent_id(self) -> None:
        """The 'executor' field maps from task.executor_agent_id."""
        app = _create_test_app()
        mock_task = _make_cancelled_task(executor_agent_id="agent-xyz")

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
            assert data["executor"] == "agent-xyz"

    @pytest.mark.asyncio
    async def test_status_is_cancelled(self) -> None:
        """The status field is 'cancelled' after successful cancel."""
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
            assert data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_completed_at_set_on_cancel(self) -> None:
        """completed_at is set when task is cancelled (terminal state)."""
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
            assert data["completed_at"] == "2026-03-07T14:35:00+00:00"

    @pytest.mark.asyncio
    async def test_result_is_null_for_cancelled_task(self) -> None:
        """Cancelled tasks have null result."""
        app = _create_test_app()
        mock_task = _make_cancelled_task(result=None)

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
            assert data["result"] is None

    @pytest.mark.asyncio
    async def test_no_principal_agent_id_field(self) -> None:
        """Response must NOT contain 'principal_agent_id' — RFC uses 'caller'."""
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
            assert "principal_agent_id" not in data
            assert "executor_agent_id" not in data


# ---------------------------------------------------------------------------
# Optional reason
# ---------------------------------------------------------------------------


class TestOptionalReason:
    """The reason field is optional in the request body."""

    @pytest.mark.asyncio
    async def test_cancel_with_reason(self) -> None:
        """Cancel with a reason passes it through to the service."""
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
                    json={"reason": "No longer needed"},
                )

            assert response.status_code == 200
            call_kwargs = mock_cancel.call_args
            assert call_kwargs.kwargs["reason"] == "No longer needed"

    @pytest.mark.asyncio
    async def test_cancel_without_reason(self) -> None:
        """Cancel without a reason passes None to the service."""
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
                    json={},
                )

            assert response.status_code == 200
            call_kwargs = mock_cancel.call_args
            assert call_kwargs.kwargs["reason"] is None

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


# ---------------------------------------------------------------------------
# HATEOAS links
# ---------------------------------------------------------------------------


class TestCancelHATEOASLinks:
    """HATEOAS _links in cancel response — terminal state, no action links."""

    @pytest.mark.asyncio
    async def test_links_contains_self_and_workflow_only(self) -> None:
        """Cancel response includes only self + workflow links (no action links)."""
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

            # Only self + workflow — no rerun, cancel, pause, etc.
            assert set(links.keys()) == {"self", "workflow"}

    @pytest.mark.asyncio
    async def test_links_use_href_object_format(self) -> None:
        """Links use {"href": "..."} object format consistent with codebase."""
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

            assert links["self"] == {"href": f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}"}
            assert links["workflow"] == {"href": f"/workflows/{WORKFLOW_ID}"}

    @pytest.mark.asyncio
    async def test_no_action_links_on_terminal_state(self) -> None:
        """Cancelled is terminal — no rerun, pause, resume links should exist."""
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

            # Explicitly assert no action links
            assert "rerun" not in links
            assert "cancel" not in links
            assert "pause" not in links
            assert "resume" not in links


# ---------------------------------------------------------------------------
# TaskEvent creation (service-level test)
# ---------------------------------------------------------------------------


class TestTaskEventCreation:
    """Verify that cancel_task creates a TaskEvent with correct data."""

    @pytest.mark.asyncio
    async def test_event_created_with_reason_and_cancelled_by(self) -> None:
        """cancel_task creates a TaskEvent with from_status, to_status, reason, cancelled_by."""
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
        mock_task.completed_at = COMPLETED_AT

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

    @pytest.mark.asyncio
    async def test_event_without_reason(self) -> None:
        """cancel_task creates a TaskEvent with reason=None when no reason provided."""
        from fleet_api.tasks.service import cancel_task as cancel_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.ACCEPTED
        mock_task.completed_at = COMPLETED_AT

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await cancel_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            cancelled_by=AGENT_ID,
            reason=None,
        )

        added_event = session.add.call_args[0][0]
        assert added_event.data["reason"] is None
        assert added_event.data["from_status"] == "accepted"


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
