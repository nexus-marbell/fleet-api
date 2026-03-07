"""Tests for POST /workflows/{workflow_id}/tasks/{task_id}/pause and /resume.

Uses FastAPI dependency overrides for auth and database session so tests
have no real database dependency. Covers:
  - Pause happy path: RUNNING task pauses -> 200
  - Pause from non-RUNNING states -> 409 TASK_NOT_PAUSABLE
  - Resume happy path: PAUSED task resumes -> 200
  - Resume from non-PAUSED states -> 409 TASK_NOT_PAUSED
  - Resume with expired TTL -> 408 PAUSE_TIMEOUT
  - Priority override on resume
  - Authorization: task caller can pause/resume, workflow owner can pause/resume
  - Unauthorized pause/resume -> 403
  - Workflow not found -> 404
  - Task not found -> 404
  - Task doesn't belong to workflow -> 404
  - TaskEvent creation for pause/resume
  - Response format: paused_state fields, _links, timestamps
  - Optional reason on pause
  - Optional priority on resume
  - Auth required
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import AuthError, ErrorCode, NotFoundError, StateError
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
OTHER_AGENT_ID = "other-agent-002"
WORKFLOW_OWNER_ID = "workflow-owner-003"
WORKFLOW_ID = "wf-cellular-automaton"
TASK_ID = "task-a1b2c3d4"
CREATED_AT = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 3, 7, 12, 5, 0, tzinfo=UTC)
PAUSED_AT = datetime(2026, 3, 7, 13, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _make_paused_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str | None = "executor-agent-xyz",
    task_input: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    paused_at: datetime | None = PAUSED_AT,
) -> MagicMock:
    """Create a mock Task in paused state."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = TaskStatus.PAUSED
    task.input = task_input if task_input is not None else {"prompt": "test"}
    task.result = None
    task.priority = TaskPriority.NORMAL
    task.created_at = CREATED_AT
    task.started_at = STARTED_AT
    task.completed_at = None
    task.paused_at = paused_at
    task.metadata_ = metadata if metadata is not None else {"progress": 42}
    return task


def _make_running_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str | None = "executor-agent-xyz",
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Task in running state."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = TaskStatus.RUNNING
    task.input = {"prompt": "test"}
    task.result = None
    task.priority = TaskPriority.NORMAL
    task.created_at = CREATED_AT
    task.started_at = STARTED_AT
    task.completed_at = None
    task.paused_at = None
    task.metadata_ = metadata if metadata is not None else {"progress": 50}
    return task


def _make_pause_event(
    task_id: str = TASK_ID,
    sequence: int = 2,
) -> MagicMock:
    """Create a mock TaskEvent for pause."""
    event = MagicMock(spec=TaskEvent)
    event.id = 100
    event.task_id = task_id
    event.event_type = "pause_requested"
    event.data = {
        "from_status": "running",
        "to_status": "paused",
        "reason": None,
        "paused_by": AGENT_ID,
        "paused_state": {
            "progress": 42,
            "message": None,
            "resumable": True,
            "state_ttl_seconds": 3600,
            "expires_at": (PAUSED_AT + timedelta(seconds=3600)).isoformat(),
        },
    }
    event.sequence = sequence
    event.created_at = PAUSED_AT
    return event


def _make_resume_event(
    task_id: str = TASK_ID,
    sequence: int = 3,
    paused_duration_seconds: int = 600,
) -> MagicMock:
    """Create a mock TaskEvent for resume."""
    event = MagicMock(spec=TaskEvent)
    event.id = 101
    event.task_id = task_id
    event.event_type = "resume_requested"
    event.data = {
        "from_status": "paused",
        "to_status": "running",
        "resumed_by": AGENT_ID,
        "paused_duration_seconds": paused_duration_seconds,
        "priority": "normal",
    }
    event.sequence = sequence
    event.created_at = PAUSED_AT + timedelta(seconds=paused_duration_seconds)
    return event


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


# ===========================================================================
# PAUSE TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# Pause happy path
# ---------------------------------------------------------------------------


class TestPauseHappyPath:
    """Pause a RUNNING task returns 200 with paused_state."""

    @pytest.mark.asyncio
    async def test_pause_running_task(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/pause on RUNNING task returns 200."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={"reason": "Need to review input"},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "paused"
            assert data["task_id"] == TASK_ID
            assert data["workflow_id"] == WORKFLOW_ID

    @pytest.mark.asyncio
    async def test_pause_response_contains_paused_at(self) -> None:
        """Pause response includes paused_at timestamp."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            data = response.json()
            assert data["paused_at"] is not None
            assert "2026-03-07" in data["paused_at"]

    @pytest.mark.asyncio
    async def test_pause_response_contains_paused_state(self) -> None:
        """Pause response includes paused_state with all required fields."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            data = response.json()
            ps = data["paused_state"]
            assert "progress" in ps
            assert "message" in ps
            assert "resumable" in ps
            assert ps["resumable"] is True
            assert "state_ttl_seconds" in ps
            assert ps["state_ttl_seconds"] == 3600
            assert "expires_at" in ps


# ---------------------------------------------------------------------------
# Pause from non-RUNNING states
# ---------------------------------------------------------------------------


class TestPauseFromNonRunningStates:
    """Pause from non-RUNNING states returns 409 TASK_NOT_PAUSABLE."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_status",
        ["accepted", "paused", "completed", "failed", "cancelled", "retasked", "redirected"],
    )
    async def test_pause_from_non_running_state(self, bad_status: str) -> None:
        """POST pause on a non-RUNNING task returns 409 TASK_NOT_PAUSABLE."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.TASK_NOT_PAUSABLE,
                message=(
                    f"Task '{TASK_ID}' cannot be paused. "
                    f"Current status: '{bad_status}'."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            assert response.status_code == 409
            data = response.json()
            assert data["code"] == "TASK_NOT_PAUSABLE"
            assert TASK_ID in data["message"]


# ---------------------------------------------------------------------------
# Pause authorization
# ---------------------------------------------------------------------------


class TestPauseAuthorization:
    """Authorization checks for task pause."""

    @pytest.mark.asyncio
    async def test_task_caller_can_pause(self) -> None:
        """Task's principal_agent_id can pause the task."""
        app = _create_test_app(agent_id=AGENT_ID)
        mock_task = _make_paused_task(principal_agent_id=AGENT_ID)
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ) as mock_pause:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={"reason": "Need review"},
                )

            assert response.status_code == 200
            call_kwargs = mock_pause.call_args
            assert call_kwargs.kwargs["paused_by"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_workflow_owner_can_pause(self) -> None:
        """Workflow owner can pause a task even if not the task caller."""
        app = _create_test_app(agent_id=WORKFLOW_OWNER_ID)
        mock_task = _make_paused_task(principal_agent_id=AGENT_ID)
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthorized_pause(self) -> None:
        """Agent who is neither task caller nor workflow owner gets 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the task caller or workflow owner may pause this task.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            assert response.status_code == 403
            data = response.json()
            assert data["code"] == "NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# Pause not found
# ---------------------------------------------------------------------------


class TestPauseNotFound:
    """404 errors for missing workflow or task on pause."""

    @pytest.mark.asyncio
    async def test_pause_workflow_not_found(self) -> None:
        """Pause on nonexistent workflow returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/workflows/wf-ghost/tasks/task-xyz/pause",
                    json={},
                )

            assert response.status_code == 404
            assert response.json()["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_pause_task_not_found(self) -> None:
        """Pause on nonexistent task returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message=f"Task 'task-ghost' not found in workflow '{WORKFLOW_ID}'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/task-ghost/pause",
                    json={},
                )

            assert response.status_code == 404
            assert response.json()["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Pause optional reason
# ---------------------------------------------------------------------------


class TestPauseOptionalReason:
    """The reason field is optional in the pause request body."""

    @pytest.mark.asyncio
    async def test_pause_with_reason(self) -> None:
        """Pause with a reason passes it through to the service."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ) as mock_pause:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={"reason": "Human needs to review"},
                )

            assert response.status_code == 200
            call_kwargs = mock_pause.call_args
            assert call_kwargs.kwargs["reason"] == "Human needs to review"

    @pytest.mark.asyncio
    async def test_pause_without_reason(self) -> None:
        """Pause without a reason passes None to the service."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ) as mock_pause:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            assert response.status_code == 200
            call_kwargs = mock_pause.call_args
            assert call_kwargs.kwargs["reason"] is None

    @pytest.mark.asyncio
    async def test_pause_with_empty_body(self) -> None:
        """Pause with no request body at all works (body is optional)."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                )

            assert response.status_code == 200


# ---------------------------------------------------------------------------
# Pause HATEOAS links
# ---------------------------------------------------------------------------


class TestPauseHATEOASLinks:
    """HATEOAS _links in pause response — paused state links."""

    @pytest.mark.asyncio
    async def test_pause_links_include_resume_cancel(self) -> None:
        """Pause response includes resume, cancel, context, redirect links."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            data = response.json()
            links = data["_links"]

            # Paused state should have resume, cancel, context, redirect
            assert "resume" in links
            assert "cancel" in links
            assert "self" in links
            assert "workflow" in links
            assert "stream" in links

    @pytest.mark.asyncio
    async def test_pause_links_resume_has_method_post(self) -> None:
        """Resume link has method POST."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            data = response.json()
            links = data["_links"]
            assert links["resume"]["method"] == "POST"
            assert links["resume"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume"


# ---------------------------------------------------------------------------
# Pause auth required
# ---------------------------------------------------------------------------


class TestPauseAuthRequired:
    """The pause endpoint requires authentication."""

    @pytest.mark.asyncio
    async def test_pause_without_auth_returns_error(self) -> None:
        """POST pause without Authorization header returns auth error."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                json={},
            )

        assert response.status_code == 401


# ===========================================================================
# RESUME TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# Resume happy path
# ---------------------------------------------------------------------------


class TestResumeHappyPath:
    """Resume a PAUSED task returns 200."""

    @pytest.mark.asyncio
    async def test_resume_paused_task(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/resume on PAUSED task returns 200."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "running"
            assert data["task_id"] == TASK_ID
            assert data["workflow_id"] == WORKFLOW_ID

    @pytest.mark.asyncio
    async def test_resume_response_contains_resumed_at(self) -> None:
        """Resume response includes resumed_at timestamp."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            data = response.json()
            assert data["resumed_at"] is not None

    @pytest.mark.asyncio
    async def test_resume_response_contains_paused_duration(self) -> None:
        """Resume response includes paused_duration_seconds."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event(paused_duration_seconds=600)

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            data = response.json()
            assert data["paused_duration_seconds"] == 600

    @pytest.mark.asyncio
    async def test_resume_response_contains_progress(self) -> None:
        """Resume response includes current progress."""
        app = _create_test_app()
        mock_task = _make_running_task(metadata={"progress": 75})
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            data = response.json()
            assert data["progress"] == 75


# ---------------------------------------------------------------------------
# Resume from non-PAUSED states
# ---------------------------------------------------------------------------


class TestResumeFromNonPausedStates:
    """Resume from non-PAUSED states returns 409 TASK_NOT_PAUSED."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_status",
        ["accepted", "running", "completed", "failed", "cancelled", "retasked", "redirected"],
    )
    async def test_resume_from_non_paused_state(self, bad_status: str) -> None:
        """POST resume on a non-PAUSED task returns 409 TASK_NOT_PAUSED."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.TASK_NOT_PAUSED,
                message=(
                    f"Task '{TASK_ID}' cannot be resumed. "
                    f"Current status: '{bad_status}'."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 409
            data = response.json()
            assert data["code"] == "TASK_NOT_PAUSED"
            assert TASK_ID in data["message"]


# ---------------------------------------------------------------------------
# Resume TTL expiry
# ---------------------------------------------------------------------------


class TestResumeTTLExpiry:
    """Resume after TTL expiry returns 408 PAUSE_TIMEOUT."""

    @pytest.mark.asyncio
    async def test_resume_after_ttl_expiry(self) -> None:
        """POST resume on expired pause returns 408 PAUSE_TIMEOUT."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.PAUSE_TIMEOUT,
                message=(
                    f"Pause TTL expired for task '{TASK_ID}'. "
                    f"Task was paused for 7200 seconds (TTL: 3600s). "
                    f"Task has been auto-cancelled."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 408
            data = response.json()
            assert data["code"] == "PAUSE_TIMEOUT"
            assert "auto-cancelled" in data["message"]


# ---------------------------------------------------------------------------
# Resume with priority override
# ---------------------------------------------------------------------------


class TestResumePriorityOverride:
    """Resume with priority override updates the task priority."""

    @pytest.mark.asyncio
    async def test_resume_with_priority_override(self) -> None:
        """POST resume with priority passes it to the service."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_task.priority = TaskPriority.HIGH
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ) as mock_resume:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={"priority": "high"},
                )

            assert response.status_code == 200
            call_kwargs = mock_resume.call_args
            assert call_kwargs.kwargs["priority"] == "high"

    @pytest.mark.asyncio
    async def test_resume_without_priority_override(self) -> None:
        """POST resume without priority passes None to the service."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ) as mock_resume:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 200
            call_kwargs = mock_resume.call_args
            assert call_kwargs.kwargs["priority"] is None


# ---------------------------------------------------------------------------
# Resume authorization
# ---------------------------------------------------------------------------


class TestResumeAuthorization:
    """Authorization checks for task resume."""

    @pytest.mark.asyncio
    async def test_task_caller_can_resume(self) -> None:
        """Task's principal_agent_id can resume the task."""
        app = _create_test_app(agent_id=AGENT_ID)
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ) as mock_resume:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 200
            call_kwargs = mock_resume.call_args
            assert call_kwargs.kwargs["resumed_by"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_workflow_owner_can_resume(self) -> None:
        """Workflow owner can resume a task even if not the task caller."""
        app = _create_test_app(agent_id=WORKFLOW_OWNER_ID)
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthorized_resume(self) -> None:
        """Agent who is neither task caller nor workflow owner gets 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the task caller or workflow owner may resume this task.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            assert response.status_code == 403
            data = response.json()
            assert data["code"] == "NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# Resume not found
# ---------------------------------------------------------------------------


class TestResumeNotFound:
    """404 errors for missing workflow or task on resume."""

    @pytest.mark.asyncio
    async def test_resume_workflow_not_found(self) -> None:
        """Resume on nonexistent workflow returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/workflows/wf-ghost/tasks/task-xyz/resume",
                    json={},
                )

            assert response.status_code == 404
            assert response.json()["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_resume_task_not_found(self) -> None:
        """Resume on nonexistent task returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message=f"Task 'task-ghost' not found in workflow '{WORKFLOW_ID}'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/task-ghost/resume",
                    json={},
                )

            assert response.status_code == 404
            assert response.json()["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Resume HATEOAS links
# ---------------------------------------------------------------------------


class TestResumeHATEOASLinks:
    """HATEOAS _links in resume response — running state links."""

    @pytest.mark.asyncio
    async def test_resume_links_include_pause_cancel(self) -> None:
        """Resume response includes pause, cancel links (running state)."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            data = response.json()
            links = data["_links"]

            # Running state should have pause, cancel, context, redirect
            assert "pause" in links
            assert "cancel" in links
            assert "self" in links
            assert "workflow" in links
            assert "stream" in links
            # Should NOT have resume (already running)
            assert "resume" not in links


# ---------------------------------------------------------------------------
# Resume auth required
# ---------------------------------------------------------------------------


class TestResumeAuthRequired:
    """The resume endpoint requires authentication."""

    @pytest.mark.asyncio
    async def test_resume_without_auth_returns_error(self) -> None:
        """POST resume without Authorization header returns auth error."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                json={},
            )

        assert response.status_code == 401


# ===========================================================================
# SERVICE-LEVEL TESTS
# ===========================================================================


# ---------------------------------------------------------------------------
# pause_task service function
# ---------------------------------------------------------------------------


class TestPauseTaskService:
    """Service-level tests for pause_task."""

    @pytest.mark.asyncio
    async def test_pause_creates_event_with_correct_data(self) -> None:
        """pause_task creates a TaskEvent with pause_requested type and paused_state data."""
        from fleet_api.tasks.service import pause_task as pause_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.started_at = STARTED_AT
        mock_task.paused_at = None
        mock_task.metadata_ = {"progress": 42}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 1
        session.execute = AsyncMock(return_value=mock_result)

        await pause_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            paused_by=AGENT_ID,
            reason="Need human review",
        )

        # Verify session.add was called with a TaskEvent
        assert session.add.called
        added_event = session.add.call_args[0][0]
        assert added_event.task_id == TASK_ID
        assert added_event.event_type == "pause_requested"
        assert added_event.data["from_status"] == "running"
        assert added_event.data["to_status"] == "paused"
        assert added_event.data["reason"] == "Need human review"
        assert added_event.data["paused_by"] == AGENT_ID
        assert added_event.data["paused_state"]["resumable"] is True
        assert added_event.sequence == 2

        # Verify paused_at was set
        assert mock_task.paused_at is not None

        # Verify commit was called
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_pause_sets_paused_at_timestamp(self) -> None:
        """pause_task sets the paused_at timestamp on the task."""
        from fleet_api.tasks.service import pause_task as pause_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.paused_at = None
        mock_task.metadata_ = {}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await pause_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            paused_by=AGENT_ID,
        )

        assert mock_task.paused_at is not None
        assert mock_task.status == TaskStatus.PAUSED


# ---------------------------------------------------------------------------
# resume_task service function
# ---------------------------------------------------------------------------


class TestResumeTaskService:
    """Service-level tests for resume_task."""

    @pytest.mark.asyncio
    async def test_resume_creates_event_with_correct_data(self) -> None:
        """resume_task creates a TaskEvent with resume_requested type."""
        from fleet_api.tasks.service import resume_task as resume_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Use a recent paused_at so TTL hasn't expired
        recent_paused_at = datetime.now(UTC) - timedelta(seconds=60)

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.PAUSED
        mock_task.paused_at = recent_paused_at
        mock_task.priority = TaskPriority.NORMAL
        mock_task.metadata_ = {"progress": 55}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 2
        session.execute = AsyncMock(return_value=mock_result)

        await resume_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            resumed_by=AGENT_ID,
        )

        assert session.add.called
        added_event = session.add.call_args[0][0]
        assert added_event.task_id == TASK_ID
        assert added_event.event_type == "resume_requested"
        assert added_event.data["from_status"] == "paused"
        assert added_event.data["to_status"] == "running"
        assert added_event.data["resumed_by"] == AGENT_ID
        assert "paused_duration_seconds" in added_event.data
        assert added_event.sequence == 3

        # Verify paused_at was cleared
        assert mock_task.paused_at is None

        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_clears_paused_at(self) -> None:
        """resume_task clears the paused_at timestamp."""
        from fleet_api.tasks.service import resume_task as resume_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Use a recent paused_at so TTL hasn't expired
        recent_paused_at = datetime.now(UTC) - timedelta(seconds=30)

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.PAUSED
        mock_task.paused_at = recent_paused_at
        mock_task.priority = TaskPriority.NORMAL
        mock_task.metadata_ = {}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await resume_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            resumed_by=AGENT_ID,
        )

        assert mock_task.paused_at is None
        assert mock_task.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_resume_with_priority_override_updates_task(self) -> None:
        """resume_task with priority override updates task.priority."""
        from fleet_api.tasks.service import resume_task as resume_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Use a recent paused_at so TTL hasn't expired
        recent_paused_at = datetime.now(UTC) - timedelta(seconds=30)

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.PAUSED
        mock_task.paused_at = recent_paused_at
        mock_task.priority = TaskPriority.NORMAL
        mock_task.metadata_ = {}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await resume_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            resumed_by=AGENT_ID,
            priority="high",
        )

        assert mock_task.priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_resume_ttl_expired_auto_cancels(self) -> None:
        """resume_task auto-cancels task when TTL has expired."""
        from fleet_api.tasks.service import resume_task as resume_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Paused 2 hours ago (TTL is 3600s = 1 hour)
        long_ago = datetime.now(UTC) - timedelta(hours=2)
        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.status = TaskStatus.PAUSED
        mock_task.paused_at = long_ago
        mock_task.metadata_ = {}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 2
        session.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(StateError) as exc_info:
            await resume_task_fn(
                session=session,
                workflow_id=WORKFLOW_ID,
                task_id=TASK_ID,
                resumed_by=AGENT_ID,
            )

        assert exc_info.value.code == ErrorCode.PAUSE_TIMEOUT
        assert "auto-cancelled" in exc_info.value.message
        # Task should have been transitioned to CANCELLED
        assert mock_task.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# Response format
# ---------------------------------------------------------------------------


class TestPauseResumeResponseFormat:
    """Verify response formats match RFC specs."""

    @pytest.mark.asyncio
    async def test_pause_response_fields(self) -> None:
        """Pause response contains all required fields."""
        app = _create_test_app()
        mock_task = _make_paused_task()
        mock_event = _make_pause_event()

        with patch(
            "fleet_api.tasks.routes.pause_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/pause",
                    json={},
                )

            data = response.json()
            expected_fields = {
                "task_id", "workflow_id", "status", "paused_at",
                "paused_state", "_links",
            }
            assert set(data.keys()) == expected_fields

    @pytest.mark.asyncio
    async def test_resume_response_fields(self) -> None:
        """Resume response contains all required fields."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                    json={},
                )

            data = response.json()
            expected_fields = {
                "task_id", "workflow_id", "status", "resumed_at",
                "paused_duration_seconds", "progress", "_links",
            }
            assert set(data.keys()) == expected_fields

    @pytest.mark.asyncio
    async def test_resume_with_empty_body(self) -> None:
        """Resume with no request body works (body is optional)."""
        app = _create_test_app()
        mock_task = _make_running_task()
        mock_event = _make_resume_event()

        with patch(
            "fleet_api.tasks.routes.resume_task",
            new_callable=AsyncMock,
            return_value=(mock_task, mock_event),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/resume",
                )

            assert response.status_code == 200
