"""Tests for POST /workflows/{workflow_id}/tasks/{task_id}/context.

Uses FastAPI dependency overrides for auth and database session so tests
have no real database dependency. Covers:
  - Happy path: inject into RUNNING task -> 202
  - Happy path: inject into PAUSED task -> 202
  - All 4 context_types (additional_input, constraint, correction, reference)
  - Urgency levels (low, normal, immediate)
  - Sequence enforcement: accept in-order, reject out-of-sequence (409)
  - Sequence enforcement: first injection (sequence=1) succeeds
  - Sequence enforcement: same sequence rejected (must be strictly >)
  - State validation: reject for COMPLETED, FAILED, CANCELLED, RETASKED, ACCEPTED, REDIRECTED
  - Auth: principal allowed, workflow owner allowed, unauthorized rejected
  - 404: workflow not found, task not found
  - Request validation: missing context_type, missing payload, missing payload.message
  - Event creation with correct data
  - HATEOAS links in response
  - Auth required (401)
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
from fleet_api.tasks.models import Task, TaskEvent, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
OTHER_AGENT_ID = "other-agent-002"
WORKFLOW_OWNER_ID = "workflow-owner-003"
WORKFLOW_ID = "wf-cellular-automaton"
TASK_ID = "task-a1b2c3d4"
CREATED_AT = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
CONTEXT_ID = "ctx-e5f6g7h8"
ACCEPTED_AT = datetime(2026, 3, 7, 14, 32, 0, tzinfo=UTC)

VALID_CONTEXT_BODY = {
    "context_type": "additional_input",
    "payload": {
        "message": "Updated parameters for analysis",
        "data": {"threshold": 0.95},
    },
    "sequence": 1,
    "urgency": "normal",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _make_context_response(
    context_id: str = CONTEXT_ID,
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    context_type: str = "additional_input",
    sequence: int = 1,
    accepted_at: str | None = None,
) -> dict[str, Any]:
    """Build a mock context injection response."""
    return {
        "context_id": context_id,
        "task_id": task_id,
        "context_type": context_type,
        "sequence": sequence,
        "status": "accepted",
        "accepted_at": accepted_at or ACCEPTED_AT.isoformat(),
        "_links": {
            "task": {"href": f"/workflows/{workflow_id}/tasks/{task_id}"},
            "stream": {"href": f"/workflows/{workflow_id}/tasks/{task_id}/stream"},
            "workflow": {"href": f"/workflows/{workflow_id}"},
        },
    }


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
# Happy path: inject into RUNNING task
# ---------------------------------------------------------------------------


class TestContextInjectionHappyPath:
    """Context injection into RUNNING and PAUSED tasks returns 202."""

    @pytest.mark.asyncio
    async def test_inject_into_running_task(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/context on RUNNING task returns 202."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_inject:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 202
            mock_inject.assert_called_once()
            data = response.json()
            assert data["status"] == "accepted"
            assert data["task_id"] == TASK_ID
            assert data["context_type"] == "additional_input"

    @pytest.mark.asyncio
    async def test_inject_into_paused_task(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/context on PAUSED task returns 202."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 202
            data = response.json()
            assert data["status"] == "accepted"


# ---------------------------------------------------------------------------
# Context types
# ---------------------------------------------------------------------------


class TestContextTypes:
    """All 4 context_types are accepted."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "context_type",
        ["additional_input", "constraint", "correction", "reference"],
    )
    async def test_all_context_types_accepted(self, context_type: str) -> None:
        """POST with context_type={context_type} returns 202."""
        app = _create_test_app()
        mock_response = _make_context_response(context_type=context_type)

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            body = {**VALID_CONTEXT_BODY, "context_type": context_type}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 202
            data = response.json()
            assert data["context_type"] == context_type


# ---------------------------------------------------------------------------
# Urgency levels
# ---------------------------------------------------------------------------


class TestUrgencyLevels:
    """Urgency levels: low, normal, immediate."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("urgency", ["low", "normal", "immediate"])
    async def test_urgency_levels_passed_to_service(self, urgency: str) -> None:
        """POST with urgency={urgency} passes it through to inject_context."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_inject:
            body = {**VALID_CONTEXT_BODY, "urgency": urgency}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 202
            call_kwargs = mock_inject.call_args
            assert call_kwargs.kwargs["urgency"] == urgency

    @pytest.mark.asyncio
    async def test_urgency_defaults_to_normal(self) -> None:
        """POST without urgency field defaults to 'normal'."""
        app = _create_test_app()
        mock_response = _make_context_response()

        body = {
            "context_type": "additional_input",
            "payload": {"message": "test"},
            "sequence": 1,
        }

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_inject:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 202
            call_kwargs = mock_inject.call_args
            assert call_kwargs.kwargs["urgency"] == "normal"


# ---------------------------------------------------------------------------
# Sequence enforcement
# ---------------------------------------------------------------------------


class TestSequenceEnforcement:
    """Context injection sequence enforcement — Sage Constraint #3."""

    @pytest.mark.asyncio
    async def test_first_injection_sequence_1_succeeds(self) -> None:
        """First context injection with sequence=1 succeeds."""
        app = _create_test_app()
        mock_response = _make_context_response(sequence=1)

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            body = {**VALID_CONTEXT_BODY, "sequence": 1}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 202
            data = response.json()
            assert data["sequence"] == 1

    @pytest.mark.asyncio
    async def test_in_order_sequence_accepted(self) -> None:
        """In-order sequence (> last) is accepted."""
        app = _create_test_app()
        mock_response = _make_context_response(sequence=3)

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            body = {**VALID_CONTEXT_BODY, "sequence": 3}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_out_of_sequence_rejected_409(self) -> None:
        """Out-of-sequence injection returns 409 CONTEXT_REJECTED."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.CONTEXT_REJECTED,
                message=(
                    f"Out-of-sequence context injection for task '{TASK_ID}'. "
                    f"Received sequence 1, but last accepted context "
                    f"sequence is 2. Sequence must be strictly greater than 2."
                ),
            ),
        ):
            body = {**VALID_CONTEXT_BODY, "sequence": 1}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 409
            data = response.json()
            assert data["code"] == "CONTEXT_REJECTED"
            assert "Out-of-sequence" in data["message"]

    @pytest.mark.asyncio
    async def test_same_sequence_rejected(self) -> None:
        """Same sequence as last accepted is rejected (must be strictly >)."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.CONTEXT_REJECTED,
                message=(
                    f"Out-of-sequence context injection for task '{TASK_ID}'. "
                    f"Received sequence 2, but last accepted context "
                    f"sequence is 2. Sequence must be strictly greater than 2."
                ),
            ),
        ):
            body = {**VALID_CONTEXT_BODY, "sequence": 2}
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=body,
                )

            assert response.status_code == 409
            data = response.json()
            assert data["code"] == "CONTEXT_REJECTED"
            assert "strictly greater" in data["message"]


# ---------------------------------------------------------------------------
# Sequence enforcement — service-level tests
# ---------------------------------------------------------------------------


class TestSequenceEnforcementService:
    """Service-level tests for inject_context sequence enforcement."""

    @pytest.mark.asyncio
    async def test_service_rejects_out_of_sequence(self) -> None:
        """inject_context raises CONTEXT_REJECTED when sequence <= last."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Mock: last context sequence is 3
        mock_context_result = MagicMock()
        mock_context_result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=mock_context_result)

        with pytest.raises(StateError) as exc_info:
            await inject_context_fn(
                session=session,
                workflow_id=WORKFLOW_ID,
                task_id=TASK_ID,
                caller_agent_id=AGENT_ID,
                context_type="additional_input",
                payload={"message": "test"},
                sequence=2,
            )

        assert exc_info.value.code == ErrorCode.CONTEXT_REJECTED
        assert "Out-of-sequence" in exc_info.value.message
        assert "strictly greater than 3" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_service_rejects_same_sequence(self) -> None:
        """inject_context raises CONTEXT_REJECTED when sequence == last."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Mock: last context sequence is 2
        mock_context_result = MagicMock()
        mock_context_result.scalar_one.return_value = 2
        session.execute = AsyncMock(return_value=mock_context_result)

        with pytest.raises(StateError) as exc_info:
            await inject_context_fn(
                session=session,
                workflow_id=WORKFLOW_ID,
                task_id=TASK_ID,
                caller_agent_id=AGENT_ID,
                context_type="additional_input",
                payload={"message": "test"},
                sequence=2,
            )

        assert exc_info.value.code == ErrorCode.CONTEXT_REJECTED
        assert "strictly greater than 2" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_service_first_injection_succeeds(self) -> None:
        """inject_context succeeds for first injection (no prior contexts)."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Two execute calls: first for max context sequence (0), then for max event sequence (5)
        mock_ctx_result = MagicMock()
        mock_ctx_result.scalar_one.return_value = 0
        mock_evt_result = MagicMock()
        mock_evt_result.scalar_one.return_value = 5
        session.execute = AsyncMock(side_effect=[mock_ctx_result, mock_evt_result])

        result = await inject_context_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            context_type="additional_input",
            payload={"message": "first injection"},
            sequence=1,
        )

        assert result["status"] == "accepted"
        assert result["task_id"] == TASK_ID
        assert result["context_type"] == "additional_input"
        assert result["sequence"] == 6  # next event sequence
        session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# State validation
# ---------------------------------------------------------------------------


class TestStateValidation:
    """State validation: reject for non-injectable statuses."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["completed", "failed", "cancelled", "retasked", "accepted", "redirected"],
    )
    async def test_reject_for_non_injectable_status(self, status: str) -> None:
        """POST context on a {status} task returns 409 CONTEXT_REJECTED."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.CONTEXT_REJECTED,
                message=(
                    f"Context injection rejected for task '{TASK_ID}'. "
                    f"Current status: '{status}'. "
                    f"Context can only be injected when task status is: paused, running."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 409
            data = response.json()
            assert data["code"] == "CONTEXT_REJECTED"
            assert status in data["message"]

    @pytest.mark.asyncio
    async def test_service_rejects_completed_task(self) -> None:
        """Service-level: inject_context raises CONTEXT_REJECTED for COMPLETED task."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.COMPLETED

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        with pytest.raises(StateError) as exc_info:
            await inject_context_fn(
                session=session,
                workflow_id=WORKFLOW_ID,
                task_id=TASK_ID,
                caller_agent_id=AGENT_ID,
                context_type="additional_input",
                payload={"message": "test"},
                sequence=1,
            )

        assert exc_info.value.code == ErrorCode.CONTEXT_REJECTED
        assert "completed" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_service_rejects_accepted_task(self) -> None:
        """Service-level: inject_context raises CONTEXT_REJECTED for ACCEPTED task."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.ACCEPTED

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        with pytest.raises(StateError) as exc_info:
            await inject_context_fn(
                session=session,
                workflow_id=WORKFLOW_ID,
                task_id=TASK_ID,
                caller_agent_id=AGENT_ID,
                context_type="additional_input",
                payload={"message": "test"},
                sequence=1,
            )

        assert exc_info.value.code == ErrorCode.CONTEXT_REJECTED
        assert "accepted" in exc_info.value.message


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestContextInjectionAuthorization:
    """Authorization checks for context injection."""

    @pytest.mark.asyncio
    async def test_task_caller_can_inject(self) -> None:
        """Task's principal_agent_id can inject context."""
        app = _create_test_app(agent_id=AGENT_ID)
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_inject:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 202
            call_kwargs = mock_inject.call_args
            assert call_kwargs.kwargs["caller_agent_id"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_workflow_owner_can_inject(self) -> None:
        """Workflow owner can inject context even if not the task caller."""
        app = _create_test_app(agent_id=WORKFLOW_OWNER_ID)
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_unauthorized_inject(self) -> None:
        """Agent who is neither task caller nor workflow owner gets 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the task caller or workflow owner may inject context.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 403
            data = response.json()
            assert data["code"] == "NOT_AUTHORIZED"
            assert "task caller or workflow owner" in data["message"]


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


class TestContextInjectionNotFound:
    """404 errors for missing workflow or task."""

    @pytest.mark.asyncio
    async def test_workflow_not_found(self) -> None:
        """Context injection on nonexistent workflow returns 404 WORKFLOW_NOT_FOUND."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/workflows/wf-ghost/tasks/task-xyz/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 404
            data = response.json()
            assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_task_not_found(self) -> None:
        """Context injection on nonexistent task returns 404 TASK_NOT_FOUND."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message=f"Task 'task-ghost' not found in workflow '{WORKFLOW_ID}'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/task-ghost/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 404
            data = response.json()
            assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    """Request body validation: missing fields return 422."""

    @pytest.mark.asyncio
    async def test_missing_context_type(self) -> None:
        """POST without context_type returns 422."""
        app = _create_test_app()

        body = {
            "payload": {"message": "test"},
            "sequence": 1,
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                json=body,
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_payload(self) -> None:
        """POST without payload returns 422."""
        app = _create_test_app()

        body = {
            "context_type": "additional_input",
            "sequence": 1,
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                json=body,
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_payload_message(self) -> None:
        """POST with payload missing 'message' returns 422."""
        app = _create_test_app()

        body = {
            "context_type": "additional_input",
            "payload": {"data": {"key": "value"}},
            "sequence": 1,
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                json=body,
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_sequence(self) -> None:
        """POST without sequence returns 422."""
        app = _create_test_app()

        body = {
            "context_type": "additional_input",
            "payload": {"message": "test"},
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                json=body,
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_sequence_must_be_positive(self) -> None:
        """POST with sequence=0 returns 422 (gt=0 constraint)."""
        app = _create_test_app()

        body = {
            "context_type": "additional_input",
            "payload": {"message": "test"},
            "sequence": 0,
        }

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                json=body,
            )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Event creation (service-level test)
# ---------------------------------------------------------------------------


class TestEventCreation:
    """Verify that inject_context creates a TaskEvent with correct data."""

    @pytest.mark.asyncio
    async def test_event_created_with_correct_data(self) -> None:
        """inject_context creates a context_injected event with all fields."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Two execute calls: max context seq (0), max event seq (3)
        mock_ctx_result = MagicMock()
        mock_ctx_result.scalar_one.return_value = 0
        mock_evt_result = MagicMock()
        mock_evt_result.scalar_one.return_value = 3
        session.execute = AsyncMock(side_effect=[mock_ctx_result, mock_evt_result])

        payload = {"message": "New constraint", "data": {"max_tokens": 1000}}

        result = await inject_context_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            context_type="constraint",
            payload=payload,
            sequence=1,
            urgency="immediate",
        )

        # Verify session.add was called with a TaskEvent
        assert session.add.called
        added_event = session.add.call_args[0][0]
        assert isinstance(added_event, TaskEvent)
        assert added_event.task_id == TASK_ID
        assert added_event.event_type == "context_injected"
        assert added_event.sequence == 4  # 3 + 1
        assert added_event.data["context_type"] == "constraint"
        assert added_event.data["payload"] == payload
        assert added_event.data["urgency"] == "immediate"
        assert added_event.data["injected_by"] == AGENT_ID
        assert "context_id" in added_event.data

        # Verify response
        assert result["status"] == "accepted"
        assert result["context_type"] == "constraint"
        assert result["sequence"] == 4

    @pytest.mark.asyncio
    async def test_event_urgency_defaults_to_normal(self) -> None:
        """inject_context defaults urgency to 'normal' in event data."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.PAUSED

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_ctx_result = MagicMock()
        mock_ctx_result.scalar_one.return_value = 0
        mock_evt_result = MagicMock()
        mock_evt_result.scalar_one.return_value = 0
        session.execute = AsyncMock(side_effect=[mock_ctx_result, mock_evt_result])

        await inject_context_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            context_type="reference",
            payload={"message": "See doc X"},
            sequence=1,
        )

        added_event = session.add.call_args[0][0]
        assert added_event.data["urgency"] == "normal"


# ---------------------------------------------------------------------------
# HATEOAS links
# ---------------------------------------------------------------------------


class TestContextInjectionLinks:
    """HATEOAS _links in context injection response."""

    @pytest.mark.asyncio
    async def test_links_contain_task_stream_workflow(self) -> None:
        """Response _links include task, stream, and workflow."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 202
            data = response.json()
            links = data["_links"]

            assert set(links.keys()) == {"task", "stream", "workflow"}

    @pytest.mark.asyncio
    async def test_links_use_correct_hrefs(self) -> None:
        """Response _links use correct href paths."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            data = response.json()
            links = data["_links"]

            assert links["task"] == {
                "href": f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}"
            }
            assert links["stream"] == {
                "href": f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream"
            }
            assert links["workflow"] == {"href": f"/workflows/{WORKFLOW_ID}"}


# ---------------------------------------------------------------------------
# Response format
# ---------------------------------------------------------------------------


class TestContextInjectionResponseFormat:
    """Response format matches RFC §3.13."""

    @pytest.mark.asyncio
    async def test_response_contains_all_fields(self) -> None:
        """Response contains: context_id, task_id, context_type, sequence,
        status, accepted_at, _links."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            assert response.status_code == 202
            data = response.json()

            expected_fields = {
                "context_id",
                "task_id",
                "context_type",
                "sequence",
                "status",
                "accepted_at",
                "_links",
            }
            assert set(data.keys()) == expected_fields

    @pytest.mark.asyncio
    async def test_context_id_format(self) -> None:
        """context_id starts with 'ctx-' prefix."""
        app = _create_test_app()
        mock_response = _make_context_response(context_id="ctx-abc12345")

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            data = response.json()
            assert data["context_id"].startswith("ctx-")

    @pytest.mark.asyncio
    async def test_status_is_accepted(self) -> None:
        """Response status is 'accepted'."""
        app = _create_test_app()
        mock_response = _make_context_response()

        with patch(
            "fleet_api.tasks.routes.inject_context",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                    json=VALID_CONTEXT_BODY,
                )

            data = response.json()
            assert data["status"] == "accepted"


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


class TestContextInjectionAuthRequired:
    """The context injection endpoint requires authentication."""

    @pytest.mark.asyncio
    async def test_inject_without_auth_returns_error(self) -> None:
        """POST context without Authorization header returns 401."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/context",
                json=VALID_CONTEXT_BODY,
            )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Service-level authorization test
# ---------------------------------------------------------------------------


class TestServiceAuthorization:
    """Service-level auth checks for inject_context."""

    @pytest.mark.asyncio
    async def test_service_rejects_unauthorized_caller(self) -> None:
        """inject_context raises NOT_AUTHORIZED for non-principal non-owner."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        with pytest.raises(AuthError) as exc_info:
            await inject_context_fn(
                session=session,
                workflow_id=WORKFLOW_ID,
                task_id=TASK_ID,
                caller_agent_id=OTHER_AGENT_ID,
                context_type="additional_input",
                payload={"message": "test"},
                sequence=1,
            )

        assert exc_info.value.code == ErrorCode.NOT_AUTHORIZED

    @pytest.mark.asyncio
    async def test_service_allows_workflow_owner(self) -> None:
        """inject_context allows the workflow owner."""
        from fleet_api.tasks.service import inject_context as inject_context_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_ctx_result = MagicMock()
        mock_ctx_result.scalar_one.return_value = 0
        mock_evt_result = MagicMock()
        mock_evt_result.scalar_one.return_value = 0
        session.execute = AsyncMock(side_effect=[mock_ctx_result, mock_evt_result])

        result = await inject_context_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=WORKFLOW_OWNER_ID,
            context_type="additional_input",
            payload={"message": "from owner"},
            sequence=1,
        )

        assert result["status"] == "accepted"
