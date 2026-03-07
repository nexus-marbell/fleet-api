"""Tests for POST /workflows/{workflow_id}/tasks/{task_id}/retask.

Uses FastAPI dependency overrides for auth and database session so tests
have no real database dependency. Covers:
  - Retask happy path from completed state -> 201
  - Retask happy path from failed state -> 201
  - Authorization: principal can retask, workflow owner can retask
  - Unauthorized retask -> 403
  - State validation: running task not retaskable -> 409
  - State validation: accepted task not retaskable -> 409
  - State validation: cancelled task not retaskable -> 409
  - State validation: already retasked task not retaskable -> 409
  - Depth limit exceeded -> 422
  - Lineage chain construction
  - Inherited context (original_input, original_result, injected_contexts)
  - Event creation on original task
  - HATEOAS _links including parent
  - Priority inheritance vs override
  - Workflow not found -> 404
  - Task not found -> 404
  - Response field names match RFC
  - Refinement stored in metadata
  - Merged input with additional_input
  - Auth required
  - Missing refinement.message -> 422
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
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
OTHER_AGENT_ID = "other-agent-002"
WORKFLOW_OWNER_ID = "workflow-owner-003"
WORKFLOW_ID = "wf-code-review"
TASK_ID = "task-x9y8z7w6"
NEW_TASK_ID = "task-r1s2t3u4"
CREATED_AT = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 3, 7, 14, 35, 0, tzinfo=UTC)
NEW_CREATED_AT = datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _make_new_task(
    task_id: str = NEW_TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    parent_task_id: str = TASK_ID,
    root_task_id: str = TASK_ID,
    lineage_depth: int = 1,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str = "executor-agent-xyz",
    priority: TaskPriority = TaskPriority.HIGH,
    task_input: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Task representing the new retasked task."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.parent_task_id = parent_task_id
    task.root_task_id = root_task_id
    task.lineage_depth = lineage_depth
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = TaskStatus.ACCEPTED
    task.input = task_input if task_input is not None else {"code": "main.py"}
    task.result = None
    task.priority = priority
    task.created_at = created_at or NEW_CREATED_AT
    task.completed_at = None
    task.timeout_seconds = 300
    task.delegation_depth = 0
    task.metadata_ = {"refinement": {"message": "Missed security concerns."}}
    return task


def _make_original_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str = "executor-agent-xyz",
    status: TaskStatus = TaskStatus.COMPLETED,
    task_input: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    lineage_depth: int = 0,
    root_task_id: str | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
) -> MagicMock:
    """Create a mock Task representing the original task being retasked."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = status
    task.input = task_input if task_input is not None else {"code": "main.py"}
    task.result = result if result is not None else {"review": "LGTM"}
    task.priority = priority
    task.created_at = CREATED_AT
    task.completed_at = COMPLETED_AT
    task.lineage_depth = lineage_depth
    task.root_task_id = root_task_id
    task.parent_task_id = None
    task.timeout_seconds = 300
    task.delegation_depth = 0
    task.metadata_ = None
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


def _retask_body(
    message: str = "The review missed security concerns.",
    additional_input: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Build a retask request body."""
    refinement: dict[str, Any] = {"message": message}
    if additional_input is not None:
        refinement["additional_input"] = additional_input
    if constraints is not None:
        refinement["constraints"] = constraints
    body: dict[str, Any] = {"refinement": refinement}
    if priority is not None:
        body["priority"] = priority
    return body


# ---------------------------------------------------------------------------
# Retask happy path — from completed
# ---------------------------------------------------------------------------


class TestRetaskFromCompleted:
    """Retask from completed state returns 201."""

    @pytest.mark.asyncio
    async def test_retask_completed_returns_201(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/retask from completed returns 201."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task(status=TaskStatus.RETASKED)

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 201
        data = response.json()
        assert data["task_id"] == NEW_TASK_ID
        assert data["parent_task_id"] == TASK_ID
        assert data["status"] == "accepted"


# ---------------------------------------------------------------------------
# Retask happy path — from failed
# ---------------------------------------------------------------------------


class TestRetaskFromFailed:
    """Retask from failed state returns 201."""

    @pytest.mark.asyncio
    async def test_retask_failed_returns_201(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/retask from failed returns 201."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task(
            status=TaskStatus.RETASKED,
            result={"error_code": "EXECUTION_FAILED", "message": "segfault"},
        )

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(message="Fix the segfault and retry"),
                )

        assert response.status_code == 201
        data = response.json()
        assert data["task_id"] == NEW_TASK_ID


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestRetaskAuthorization:
    """Authorization checks for task retask."""

    @pytest.mark.asyncio
    async def test_principal_can_retask(self) -> None:
        """Task's principal_agent_id can retask the task."""
        app = _create_test_app(agent_id=AGENT_ID)
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_retask,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 201
        call_kwargs = mock_retask.call_args
        assert call_kwargs.kwargs["caller_agent_id"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_workflow_owner_can_retask(self) -> None:
        """Workflow owner can retask a task even if not the task caller."""
        app = _create_test_app(agent_id=WORKFLOW_OWNER_ID)
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_unauthorized_retask(self) -> None:
        """Agent who is neither task caller nor workflow owner gets 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the task caller or workflow owner may retask this task.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# State validation — non-retaskable states
# ---------------------------------------------------------------------------


class TestRetaskStateValidation:
    """Tasks not in completed/failed state cannot be retasked."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["running", "accepted", "paused"])
    async def test_active_task_not_retaskable(self, status: str) -> None:
        """POST retask on an active ({status}) task returns 409."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.RETASK_NOT_REVIEWABLE,
                message=(
                    f"Task '{TASK_ID}' cannot be retasked. "
                    f"Current status: '{status}'. "
                    f"Only tasks with status 'completed' or 'failed' can be retasked."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "RETASK_NOT_REVIEWABLE"
        assert status in data["message"]

    @pytest.mark.asyncio
    async def test_cancelled_task_not_retaskable(self) -> None:
        """POST retask on a cancelled task returns 409."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.RETASK_NOT_REVIEWABLE,
                message=(
                    f"Task '{TASK_ID}' cannot be retasked. "
                    f"Current status: 'cancelled'. "
                    f"Only tasks with status 'completed' or 'failed' can be retasked."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "RETASK_NOT_REVIEWABLE"

    @pytest.mark.asyncio
    async def test_already_retasked_not_retaskable(self) -> None:
        """POST retask on an already retasked task returns 409."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.RETASK_NOT_REVIEWABLE,
                message=(
                    f"Task '{TASK_ID}' cannot be retasked. "
                    f"Current status: 'retasked'. "
                    f"Only tasks with status 'completed' or 'failed' can be retasked."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "RETASK_NOT_REVIEWABLE"


# ---------------------------------------------------------------------------
# Depth limit
# ---------------------------------------------------------------------------


class TestRetaskDepthLimit:
    """Retask depth exceeded returns 422."""

    @pytest.mark.asyncio
    async def test_depth_limit_exceeded(self) -> None:
        """POST retask when lineage_depth >= max returns 422."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=InputValidationError(
                code=ErrorCode.RETASK_DEPTH_EXCEEDED,
                message=(
                    "Retask depth limit (10) exceeded. "
                    f"Task '{TASK_ID}' is already at depth 10."
                ),
                suggestion=(
                    "The retask chain has reached its maximum depth. "
                    "Start a new task instead."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 422
        data = response.json()
        assert data["code"] == "RETASK_DEPTH_EXCEEDED"
        assert "depth" in data["message"].lower()


# ---------------------------------------------------------------------------
# Lineage chain
# ---------------------------------------------------------------------------


class TestRetaskLineage:
    """Lineage information in retask response."""

    @pytest.mark.asyncio
    async def test_lineage_depth_and_root(self) -> None:
        """Response contains lineage with correct depth and root_task_id."""
        app = _create_test_app()
        new_task = _make_new_task(lineage_depth=1, root_task_id=TASK_ID)
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        lineage = data["lineage"]
        assert lineage["depth"] == 1
        assert lineage["root_task_id"] == TASK_ID
        assert lineage["chain"] == [TASK_ID, NEW_TASK_ID]

    @pytest.mark.asyncio
    async def test_lineage_chain_depth_2(self) -> None:
        """Lineage chain at depth 2 includes 3 tasks."""
        app = _create_test_app()
        root_id = "task-root0000"
        parent_id = TASK_ID
        new_task = _make_new_task(
            lineage_depth=2,
            root_task_id=root_id,
            parent_task_id=parent_id,
        )
        original_task = _make_original_task(
            root_task_id=root_id,
            lineage_depth=1,
        )

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[root_id, parent_id, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        lineage = data["lineage"]
        assert lineage["depth"] == 2
        assert lineage["root_task_id"] == root_id
        assert len(lineage["chain"]) == 3
        assert lineage["chain"][0] == root_id
        assert lineage["chain"][-1] == NEW_TASK_ID


# ---------------------------------------------------------------------------
# Inherited context
# ---------------------------------------------------------------------------


class TestRetaskInheritedContext:
    """Inherited context block in retask response."""

    @pytest.mark.asyncio
    async def test_inherited_context_with_result(self) -> None:
        """When original has a result, original_result is true."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task(result={"review": "LGTM"})

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=1,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        ctx = data["inherited_context"]
        assert ctx["original_input"] is True
        assert ctx["original_result"] is True
        assert ctx["injected_contexts"] == 1

    @pytest.mark.asyncio
    async def test_inherited_context_no_result(self) -> None:
        """When original has no result, original_result is false."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task(result=None)
        original_task.result = None

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        ctx = data["inherited_context"]
        assert ctx["original_result"] is False
        assert ctx["injected_contexts"] == 0


# ---------------------------------------------------------------------------
# Event creation (service-level test)
# ---------------------------------------------------------------------------


class TestRetaskEventCreation:
    """Verify that retask_task creates events with correct data."""

    @pytest.mark.asyncio
    async def test_status_event_on_original_task(self) -> None:
        """retask_task creates a status event on the original task with retask info."""
        from fleet_api.tasks.service import retask_task as retask_task_fn

        session = AsyncMock()

        # Mock workflow
        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Mock task
        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = "executor-agent-xyz"
        mock_task.status = TaskStatus.COMPLETED
        mock_task.input = {"code": "main.py"}
        mock_task.result = {"review": "LGTM"}
        mock_task.priority = TaskPriority.NORMAL
        mock_task.lineage_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0
        mock_task.completed_at = COMPLETED_AT

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        # session.get returns workflow first, then task
        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Mock the sequence query
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=mock_result)

        refinement = {"message": "Missed security concerns."}
        await retask_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            refinement=refinement,
        )

        # Verify session.add was called multiple times (new_task + event + new_event)
        assert session.add.call_count == 3

        # The second add call should be the status event on original task
        status_event = session.add.call_args_list[1][0][0]
        assert status_event.task_id == TASK_ID
        assert status_event.event_type == "status"
        assert status_event.data["from_status"] == "completed"
        assert status_event.data["to_status"] == "retasked"
        assert status_event.data["retasked_by"] == AGENT_ID
        assert status_event.data["refinement_message"] == "Missed security concerns."
        assert "retask_id" in status_event.data
        assert status_event.sequence == 4  # 3 (last) + 1

        # Verify commit was called
        session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# HATEOAS links
# ---------------------------------------------------------------------------


class TestRetaskHATEOASLinks:
    """HATEOAS _links in retask response."""

    @pytest.mark.asyncio
    async def test_links_include_parent_and_standard(self) -> None:
        """Retask response includes self, workflow, stream, parent, and action links."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        links = data["_links"]

        # Standard links
        assert "self" in links
        assert "workflow" in links
        assert "stream" in links

        # Parent link
        assert "parent" in links
        assert links["parent"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}"

        # Self link points to new task
        assert links["self"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{NEW_TASK_ID}"

        # Accepted status should have cancel action link
        assert "cancel" in links
        assert links["cancel"]["method"] == "POST"

    @pytest.mark.asyncio
    async def test_links_use_correct_href_format(self) -> None:
        """Links use {"href": "..."} format per RFC."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        links = data["_links"]

        # Non-action links: just href
        assert isinstance(links["self"], dict)
        assert "href" in links["self"]
        assert isinstance(links["parent"], dict)
        assert "href" in links["parent"]


# ---------------------------------------------------------------------------
# Priority inheritance vs override
# ---------------------------------------------------------------------------


class TestRetaskPriority:
    """Priority handling in retask."""

    @pytest.mark.asyncio
    async def test_priority_override(self) -> None:
        """When priority is provided, it overrides the original's priority."""
        app = _create_test_app()
        new_task = _make_new_task(priority=TaskPriority.HIGH)
        original_task = _make_original_task(priority=TaskPriority.NORMAL)

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_retask,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(priority="high"),
                )

        assert response.status_code == 201
        data = response.json()
        assert data["priority"] == "high"
        call_kwargs = mock_retask.call_args
        assert call_kwargs.kwargs["priority"] == "high"

    @pytest.mark.asyncio
    async def test_priority_inherited_when_not_specified(self) -> None:
        """When priority is not provided, new task inherits from original."""
        app = _create_test_app()
        new_task = _make_new_task(priority=TaskPriority.NORMAL)
        original_task = _make_original_task(priority=TaskPriority.NORMAL)

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_retask,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 201
        data = response.json()
        assert data["priority"] == "normal"
        call_kwargs = mock_retask.call_args
        assert call_kwargs.kwargs["priority"] is None


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


class TestRetaskNotFound:
    """404 errors for missing workflow or task."""

    @pytest.mark.asyncio
    async def test_workflow_not_found(self) -> None:
        """Retask on nonexistent workflow returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/workflows/wf-ghost/tasks/task-xyz/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_task_not_found(self) -> None:
        """Retask on nonexistent task returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.retask_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message=f"Task 'task-ghost' not found in workflow '{WORKFLOW_ID}'.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/task-ghost/retask",
                    json=_retask_body(),
                )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Response format
# ---------------------------------------------------------------------------


class TestRetaskResponseFormat:
    """Response field names match RFC spec."""

    @pytest.mark.asyncio
    async def test_response_contains_all_required_fields(self) -> None:
        """Response contains task_id, parent_task_id, workflow_id, status,
        caller, executor, priority, created_at, lineage, inherited_context, _links."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        expected_fields = {
            "task_id", "parent_task_id", "workflow_id", "status",
            "caller", "executor", "priority", "created_at",
            "lineage", "inherited_context", "_links",
        }
        assert expected_fields.issubset(set(data.keys()))

    @pytest.mark.asyncio
    async def test_no_internal_field_names(self) -> None:
        """Response uses RFC field names, not internal model column names."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(),
                )

        data = response.json()
        assert "principal_agent_id" not in data
        assert "executor_agent_id" not in data
        assert "caller" in data
        assert "executor" in data


# ---------------------------------------------------------------------------
# Merged input with additional_input
# ---------------------------------------------------------------------------


class TestRetaskMergedInput:
    """Input merging with additional_input from refinement."""

    @pytest.mark.asyncio
    async def test_additional_input_passed_in_refinement(self) -> None:
        """Refinement additional_input is passed to retask_task."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_retask,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(
                        additional_input={"security_checks": ["xss", "sql_injection"]},
                    ),
                )

        assert response.status_code == 201
        call_kwargs = mock_retask.call_args
        refinement = call_kwargs.kwargs["refinement"]
        assert refinement["additional_input"] == {"security_checks": ["xss", "sql_injection"]}

    @pytest.mark.asyncio
    async def test_constraints_passed_in_refinement(self) -> None:
        """Refinement constraints are passed to retask_task."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.retask_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_retask,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
            patch(
                "fleet_api.tasks.routes.count_context_injections",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                    json=_retask_body(
                        constraints={"max_time": 60, "require_tests": True},
                    ),
                )

        assert response.status_code == 201
        call_kwargs = mock_retask.call_args
        refinement = call_kwargs.kwargs["refinement"]
        assert refinement["constraints"] == {"max_time": 60, "require_tests": True}


# ---------------------------------------------------------------------------
# Auth required
# ---------------------------------------------------------------------------


class TestRetaskAuthRequired:
    """The retask endpoint requires authentication."""

    @pytest.mark.asyncio
    async def test_retask_without_auth_returns_error(self) -> None:
        """POST retask without Authorization header returns auth error."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                json=_retask_body(),
            )

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Missing refinement message (validation)
# ---------------------------------------------------------------------------


class TestRetaskValidation:
    """Request validation for the retask endpoint."""

    @pytest.mark.asyncio
    async def test_missing_refinement_message_returns_422(self) -> None:
        """POST retask without refinement.message returns 422."""
        app = _create_test_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                json={"refinement": {}},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_refinement_object_returns_422(self) -> None:
        """POST retask without refinement object returns 422."""
        app = _create_test_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
                json={},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_body_returns_422(self) -> None:
        """POST retask with no body returns 422."""
        app = _create_test_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/retask",
            )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Service-level unit tests
# ---------------------------------------------------------------------------


class TestRetaskServiceUnit:
    """Unit tests for the retask_task service function."""

    @pytest.mark.asyncio
    async def test_retask_creates_new_task_with_correct_lineage(self) -> None:
        """retask_task creates a new task with correct parent and root."""
        from fleet_api.tasks.service import retask_task as retask_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = "executor-agent-xyz"
        mock_task.status = TaskStatus.COMPLETED
        mock_task.input = {"code": "main.py"}
        mock_task.result = {"review": "LGTM"}
        mock_task.priority = TaskPriority.NORMAL
        mock_task.lineage_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await retask_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            refinement={"message": "Fix it"},
        )

        # First add call is the new task
        new_task_call = session.add.call_args_list[0][0][0]
        assert new_task_call.parent_task_id == TASK_ID
        assert new_task_call.root_task_id == TASK_ID  # first in chain
        assert new_task_call.lineage_depth == 1
        assert new_task_call.status == TaskStatus.ACCEPTED
        assert new_task_call.principal_agent_id == AGENT_ID
        assert new_task_call.executor_agent_id == "executor-agent-xyz"

    @pytest.mark.asyncio
    async def test_retask_transitions_original_to_retasked(self) -> None:
        """retask_task transitions the original task to RETASKED status."""
        from fleet_api.tasks.service import retask_task as retask_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = "executor-agent-xyz"
        mock_task.status = TaskStatus.FAILED
        mock_task.input = {"code": "main.py"}
        mock_task.result = None
        mock_task.priority = TaskPriority.HIGH
        mock_task.lineage_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await retask_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            refinement={"message": "Try again"},
        )

        # Original task should have been transitioned
        mock_task.transition_to.assert_called_once_with(TaskStatus.RETASKED)
        assert mock_task.status == TaskStatus.RETASKED


# ---------------------------------------------------------------------------
# Build lineage chain unit test
# ---------------------------------------------------------------------------


class TestBuildLineageChain:
    """Unit tests for build_lineage_chain."""

    @pytest.mark.asyncio
    async def test_single_retask_chain(self) -> None:
        """Chain with depth 1 returns [parent, current]."""
        from fleet_api.tasks.service import build_lineage_chain

        session = AsyncMock()

        # Current task (depth 1)
        current_task = MagicMock(spec=Task)
        current_task.id = NEW_TASK_ID
        current_task.parent_task_id = TASK_ID
        current_task.lineage_depth = 1

        # Parent task (depth 0)
        parent_task = MagicMock(spec=Task)
        parent_task.id = TASK_ID
        parent_task.parent_task_id = None

        session.get = AsyncMock(return_value=parent_task)

        chain = await build_lineage_chain(session, current_task)
        assert chain == [TASK_ID, NEW_TASK_ID]

    @pytest.mark.asyncio
    async def test_root_task_chain(self) -> None:
        """Task at depth 0 returns [self]."""
        from fleet_api.tasks.service import build_lineage_chain

        session = AsyncMock()

        task = MagicMock(spec=Task)
        task.id = TASK_ID
        task.parent_task_id = None
        task.lineage_depth = 0

        chain = await build_lineage_chain(session, task)
        assert chain == [TASK_ID]


# ---------------------------------------------------------------------------
# Count context injections unit test
# ---------------------------------------------------------------------------


class TestCountContextInjections:
    """Unit tests for count_context_injections."""

    @pytest.mark.asyncio
    async def test_counts_injected_events(self) -> None:
        """Counts context_injected events on a task."""
        from fleet_api.tasks.service import count_context_injections

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 3
        session.execute = AsyncMock(return_value=mock_result)

        count = await count_context_injections(session, TASK_ID)
        assert count == 3

    @pytest.mark.asyncio
    async def test_zero_when_no_injections(self) -> None:
        """Returns 0 when no context_injected events exist."""
        from fleet_api.tasks.service import count_context_injections

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        count = await count_context_injections(session, TASK_ID)
        assert count == 0
