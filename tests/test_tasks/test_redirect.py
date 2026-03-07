"""Tests for POST /workflows/{workflow_id}/tasks/{task_id}/redirect.

Uses FastAPI dependency overrides for auth and database session so tests
have no real database dependency. Covers:
  - Redirect happy path from running state -> 201
  - Redirect happy path from paused state -> 201
  - Authorization: principal can redirect, workflow owner can redirect
  - Unauthorized redirect -> 403
  - State validation: completed task not redirectable -> 409
  - State validation: failed task not redirectable -> 409
  - State validation: cancelled task not redirectable -> 409
  - State validation: accepted task not redirectable -> 409
  - State validation: retasked task not redirectable -> 409
  - State validation: already redirected task not redirectable -> 409
  - Lineage: redirected_from field, chain at depth 1
  - Lineage: chained redirect (depth 2)
  - inherit_progress true vs false
  - Priority: explicit override vs inherited
  - Response format: RFC field names (caller, executor)
  - HATEOAS links including redirected_from
  - Event creation on original (redirected) and new task (created)
  - Request validation: missing reason, missing new_input
  - Workflow not found -> 404
  - Task not found -> 404
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
from fleet_api.errors import (
    AuthError,
    ErrorCode,
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
WORKFLOW_ID = "wf-cellular-automaton"
TASK_ID = "task-a1b2c3d4"
NEW_TASK_ID = "task-new12345"
EXECUTOR_ID = "grok-pi-dev"
CREATED_AT = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
NEW_CREATED_AT = datetime(2026, 3, 7, 14, 32, 0, tzinfo=UTC)


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
    retask_depth: int = 1,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str = EXECUTOR_ID,
    priority: TaskPriority = TaskPriority.HIGH,
    task_input: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Task representing the new redirected task."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.parent_task_id = parent_task_id
    task.root_task_id = root_task_id
    task.retask_depth = retask_depth
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = TaskStatus.ACCEPTED
    task.input = task_input if task_input is not None else {"prompt": "new task input"}
    task.result = None
    task.priority = priority
    task.created_at = created_at or NEW_CREATED_AT
    task.completed_at = None
    task.timeout_seconds = 300
    task.delegation_depth = 0
    task.metadata_ = metadata if metadata is not None else {"redirect_reason": "scope changed"}
    return task


def _make_original_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str = EXECUTOR_ID,
    status: TaskStatus = TaskStatus.REDIRECTED,
    task_input: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    retask_depth: int = 0,
    root_task_id: str | None = None,
    priority: TaskPriority = TaskPriority.HIGH,
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Task representing the original task being redirected."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = status
    task.input = task_input if task_input is not None else {"prompt": "original input"}
    task.result = result
    task.priority = priority
    task.created_at = CREATED_AT
    task.completed_at = None
    task.retask_depth = retask_depth
    task.root_task_id = root_task_id
    task.parent_task_id = None
    task.timeout_seconds = 300
    task.delegation_depth = 0
    task.metadata_ = metadata
    task.paused_at = None
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


def _redirect_body(
    reason: str = "Scope changed, need different analysis.",
    new_input: dict[str, Any] | None = None,
    inherit_progress: bool = False,
    priority: str | None = None,
) -> dict[str, Any]:
    """Build a redirect request body."""
    body: dict[str, Any] = {
        "reason": reason,
        "new_input": new_input if new_input is not None else {"prompt": "new analysis target"},
    }
    if inherit_progress:
        body["inherit_progress"] = True
    if priority is not None:
        body["priority"] = priority
    return body


def _url(workflow_id: str = WORKFLOW_ID, task_id: str = TASK_ID) -> str:
    """Build the redirect endpoint URL."""
    return f"/workflows/{workflow_id}/tasks/{task_id}/redirect"


# ---------------------------------------------------------------------------
# Redirect happy path — from RUNNING
# ---------------------------------------------------------------------------


class TestRedirectFromRunning:
    """Redirect from running state returns 201."""

    @pytest.mark.asyncio
    async def test_redirect_running_returns_201(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/redirect from running returns 201."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task(status=TaskStatus.REDIRECTED)

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 201
        data = response.json()
        assert data["task_id"] == NEW_TASK_ID
        assert data["workflow_id"] == WORKFLOW_ID
        assert data["status"] == "accepted"
        assert data["redirected_from"] == TASK_ID

    @pytest.mark.asyncio
    async def test_redirect_running_response_fields(self) -> None:
        """Redirect response has all RFC-required fields."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        # RFC field names
        assert data["caller"] == AGENT_ID
        assert data["executor"] == EXECUTOR_ID
        assert data["priority"] == "high"
        assert "created_at" in data
        assert "lineage" in data
        assert "_links" in data


# ---------------------------------------------------------------------------
# Redirect happy path — from PAUSED
# ---------------------------------------------------------------------------


class TestRedirectFromPaused:
    """Redirect from paused state returns 201."""

    @pytest.mark.asyncio
    async def test_redirect_paused_returns_201(self) -> None:
        """POST /workflows/{wf}/tasks/{task}/redirect from paused returns 201."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task(status=TaskStatus.REDIRECTED)

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 201
        data = response.json()
        assert data["task_id"] == NEW_TASK_ID
        assert data["redirected_from"] == TASK_ID


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestRedirectAuthorization:
    """Authorization checks for task redirect."""

    @pytest.mark.asyncio
    async def test_principal_can_redirect(self) -> None:
        """Task's principal_agent_id can redirect the task."""
        app = _create_test_app(agent_id=AGENT_ID)
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_redirect,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 201
        call_kwargs = mock_redirect.call_args
        assert call_kwargs.kwargs["caller_agent_id"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_workflow_owner_can_redirect(self) -> None:
        """Workflow owner can redirect a task even if not the task caller."""
        app = _create_test_app(agent_id=WORKFLOW_OWNER_ID)
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_unauthorized_redirect(self) -> None:
        """Agent who is neither task caller nor workflow owner gets 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        with patch(
            "fleet_api.tasks.routes.redirect_task",
            new_callable=AsyncMock,
            side_effect=AuthError(
                code=ErrorCode.NOT_AUTHORIZED,
                message="Only the task caller or workflow owner may redirect this task.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# State validation — non-redirectable states
# ---------------------------------------------------------------------------


class TestRedirectStateValidation:
    """Tasks not in running/paused state cannot be redirected."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["completed", "failed", "cancelled", "accepted", "retasked", "redirected"],
    )
    async def test_invalid_state_not_redirectable(self, status: str) -> None:
        """POST redirect on a task in {status} state returns 409."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.redirect_task",
            new_callable=AsyncMock,
            side_effect=StateError(
                code=ErrorCode.REDIRECT_NOT_POSSIBLE,
                message=(
                    f"Task '{TASK_ID}' cannot be redirected. "
                    f"Current status: '{status}'. "
                    f"Only tasks with status 'running' or 'paused' can be redirected."
                ),
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "REDIRECT_NOT_POSSIBLE"
        assert status in data["message"]


# ---------------------------------------------------------------------------
# 404 — workflow and task not found
# ---------------------------------------------------------------------------


class TestRedirectNotFound:
    """404 errors for missing workflow or task."""

    @pytest.mark.asyncio
    async def test_workflow_not_found(self) -> None:
        """POST redirect on non-existent workflow returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.redirect_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-nonexistent' not found.",
                suggestion="Check the workflow ID.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _url(workflow_id="wf-nonexistent"),
                    json=_redirect_body(),
                )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_task_not_found(self) -> None:
        """POST redirect on non-existent task returns 404."""
        app = _create_test_app()

        with patch(
            "fleet_api.tasks.routes.redirect_task",
            new_callable=AsyncMock,
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message="Task 'task-nonexistent' not found in workflow.",
                suggestion="Check the task ID.",
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _url(task_id="task-nonexistent"),
                    json=_redirect_body(),
                )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


class TestRedirectLineage:
    """Lineage information in redirect response."""

    @pytest.mark.asyncio
    async def test_lineage_depth_1(self) -> None:
        """Response contains lineage with depth 1 for first redirect."""
        app = _create_test_app()
        new_task = _make_new_task(retask_depth=1, root_task_id=TASK_ID)
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        lineage = data["lineage"]
        assert lineage["depth"] == 1
        assert lineage["root_task_id"] == TASK_ID
        assert lineage["chain"] == [TASK_ID, NEW_TASK_ID]

    @pytest.mark.asyncio
    async def test_lineage_chained_redirect_depth_2(self) -> None:
        """Lineage chain at depth 2 (redirect of a previously-redirected task's new task)."""
        app = _create_test_app()
        root_id = "task-root0000"
        parent_id = TASK_ID
        new_task = _make_new_task(
            retask_depth=2,
            root_task_id=root_id,
            parent_task_id=parent_id,
        )
        original_task = _make_original_task(
            root_task_id=root_id,
            retask_depth=1,
        )

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[root_id, parent_id, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        lineage = data["lineage"]
        assert lineage["depth"] == 2
        assert lineage["root_task_id"] == root_id
        assert len(lineage["chain"]) == 3
        assert lineage["chain"][0] == root_id
        assert lineage["chain"][-1] == NEW_TASK_ID

    @pytest.mark.asyncio
    async def test_redirected_from_field(self) -> None:
        """Response includes redirected_from with original task ID."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        assert data["redirected_from"] == TASK_ID


# ---------------------------------------------------------------------------
# inherit_progress
# ---------------------------------------------------------------------------


class TestRedirectInheritProgress:
    """Test inherit_progress behavior."""

    @pytest.mark.asyncio
    async def test_inherit_progress_false_default(self) -> None:
        """When inherit_progress is false (default), progress metadata is not inherited."""
        app = _create_test_app()
        new_task = _make_new_task(metadata={"redirect_reason": "scope changed"})
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_redirect,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 201
        call_kwargs = mock_redirect.call_args
        assert call_kwargs.kwargs["inherit_progress"] is False

    @pytest.mark.asyncio
    async def test_inherit_progress_true(self) -> None:
        """When inherit_progress is true, it is passed to the service."""
        app = _create_test_app()
        new_task = _make_new_task(
            metadata={"redirect_reason": "scope changed", "progress": 42}
        )
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_redirect,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _url(),
                    json=_redirect_body(inherit_progress=True),
                )

        assert response.status_code == 201
        call_kwargs = mock_redirect.call_args
        assert call_kwargs.kwargs["inherit_progress"] is True


# ---------------------------------------------------------------------------
# Priority: explicit override vs inherited
# ---------------------------------------------------------------------------


class TestRedirectPriority:
    """Priority handling in redirect."""

    @pytest.mark.asyncio
    async def test_priority_inherited_when_omitted(self) -> None:
        """When priority is omitted, it inherits from original task."""
        app = _create_test_app()
        new_task = _make_new_task(priority=TaskPriority.HIGH)
        original_task = _make_original_task(priority=TaskPriority.HIGH)

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_redirect,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 201
        data = response.json()
        assert data["priority"] == "high"
        call_kwargs = mock_redirect.call_args
        assert call_kwargs.kwargs["priority"] is None

    @pytest.mark.asyncio
    async def test_priority_explicit_override(self) -> None:
        """When priority is specified, it overrides the original."""
        app = _create_test_app()
        new_task = _make_new_task(priority=TaskPriority.CRITICAL)
        original_task = _make_original_task(priority=TaskPriority.HIGH)

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ) as mock_redirect,
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    _url(),
                    json=_redirect_body(priority="critical"),
                )

        assert response.status_code == 201
        data = response.json()
        assert data["priority"] == "critical"
        call_kwargs = mock_redirect.call_args
        assert call_kwargs.kwargs["priority"] == "critical"


# ---------------------------------------------------------------------------
# HATEOAS links
# ---------------------------------------------------------------------------


class TestRedirectHATEOASLinks:
    """HATEOAS _links in redirect response."""

    @pytest.mark.asyncio
    async def test_links_include_redirected_from_and_standard(self) -> None:
        """Redirect response includes self, workflow, stream, and redirected_from links."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        links = data["_links"]

        # Standard links
        assert "self" in links
        assert links["self"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{NEW_TASK_ID}"
        assert "workflow" in links
        assert links["workflow"]["href"] == f"/workflows/{WORKFLOW_ID}"
        assert "stream" in links
        assert links["stream"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{NEW_TASK_ID}/stream"

        # Redirect-specific link
        assert "redirected_from" in links
        assert links["redirected_from"]["href"] == f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}"

    @pytest.mark.asyncio
    async def test_new_task_links_include_cancel(self) -> None:
        """New task (accepted status) should have cancel action link."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        links = data["_links"]
        assert "cancel" in links
        assert links["cancel"]["method"] == "POST"


# ---------------------------------------------------------------------------
# Event creation (service-level test)
# ---------------------------------------------------------------------------


class TestRedirectEventCreation:
    """Verify that redirect_task creates events with correct data."""

    @pytest.mark.asyncio
    async def test_status_event_on_original_task(self) -> None:
        """redirect_task creates a status event on the original task with redirect info."""
        from fleet_api.tasks.service import redirect_task as redirect_task_fn

        session = AsyncMock()

        # Mock workflow
        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        # Mock task
        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = EXECUTOR_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.input = {"prompt": "original input"}
        mock_task.result = None
        mock_task.priority = TaskPriority.HIGH
        mock_task.retask_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0
        mock_task.completed_at = None
        mock_task.metadata_ = {"progress": 42}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)

        # session.get returns workflow first, then task
        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        # Mock the sequence query
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 5
        session.execute = AsyncMock(return_value=mock_result)

        await redirect_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            reason="Scope changed.",
            new_input={"prompt": "new analysis"},
        )

        # Verify session.add was called 3 times (new_task + event + new_event)
        assert session.add.call_count == 3

        # The second add call should be the status event on original task
        status_event = session.add.call_args_list[1][0][0]
        assert status_event.task_id == TASK_ID
        assert status_event.event_type == "status"
        assert status_event.data["from_status"] == "running"
        assert status_event.data["to_status"] == "redirected"
        assert status_event.data["redirected_by"] == AGENT_ID
        assert status_event.data["reason"] == "Scope changed."
        assert "redirect_id" in status_event.data
        assert status_event.sequence == 6  # 5 (last) + 1

        # The third add call should be the created event on new task
        created_event = session.add.call_args_list[2][0][0]
        assert created_event.event_type == "created"
        assert created_event.sequence == 1
        assert created_event.data["status"] == "accepted"
        assert created_event.data["caller"] == AGENT_ID
        assert created_event.data["redirect_of"] == TASK_ID

        # Verify commit was called
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_new_task_uses_new_input_not_original(self) -> None:
        """redirect_task creates new task with new_input, not original input."""
        from fleet_api.tasks.service import redirect_task as redirect_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = EXECUTOR_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.input = {"prompt": "original input"}
        mock_task.result = None
        mock_task.priority = TaskPriority.NORMAL
        mock_task.retask_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0
        mock_task.completed_at = None
        mock_task.metadata_ = None

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)
        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        new_input = {"prompt": "totally different input", "target": "new-target"}

        await redirect_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            reason="Different analysis needed.",
            new_input=new_input,
        )

        # First add call is the new task
        new_task = session.add.call_args_list[0][0][0]
        assert new_task.input == new_input
        assert new_task.input != mock_task.input

    @pytest.mark.asyncio
    async def test_inherit_progress_copies_metadata(self) -> None:
        """When inherit_progress=True, progress metadata is copied to new task."""
        from fleet_api.tasks.service import redirect_task as redirect_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = EXECUTOR_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.input = {"prompt": "original"}
        mock_task.result = None
        mock_task.priority = TaskPriority.NORMAL
        mock_task.retask_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0
        mock_task.completed_at = None
        mock_task.metadata_ = {"progress": 75, "progress_message": "3/4 done"}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)
        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await redirect_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            reason="Redirect with progress.",
            new_input={"prompt": "new"},
            inherit_progress=True,
        )

        new_task = session.add.call_args_list[0][0][0]
        assert new_task.metadata_["progress"] == 75
        assert new_task.metadata_["progress_message"] == "3/4 done"
        assert new_task.metadata_["redirect_reason"] == "Redirect with progress."

    @pytest.mark.asyncio
    async def test_inherit_progress_false_no_progress_metadata(self) -> None:
        """When inherit_progress=False, progress metadata is NOT copied."""
        from fleet_api.tasks.service import redirect_task as redirect_task_fn

        session = AsyncMock()

        mock_workflow = MagicMock()
        mock_workflow.owner_agent_id = WORKFLOW_OWNER_ID

        mock_task = MagicMock(spec=Task)
        mock_task.id = TASK_ID
        mock_task.workflow_id = WORKFLOW_ID
        mock_task.principal_agent_id = AGENT_ID
        mock_task.executor_agent_id = EXECUTOR_ID
        mock_task.status = TaskStatus.RUNNING
        mock_task.input = {"prompt": "original"}
        mock_task.result = None
        mock_task.priority = TaskPriority.NORMAL
        mock_task.retask_depth = 0
        mock_task.root_task_id = None
        mock_task.parent_task_id = None
        mock_task.timeout_seconds = 300
        mock_task.delegation_depth = 0
        mock_task.completed_at = None
        mock_task.metadata_ = {"progress": 75, "progress_message": "3/4 done"}

        def mock_transition(new_status: TaskStatus) -> None:
            mock_task.status = new_status

        mock_task.transition_to = MagicMock(side_effect=mock_transition)
        session.get = AsyncMock(side_effect=[mock_workflow, mock_task])

        mock_result = MagicMock()
        mock_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=mock_result)

        await redirect_task_fn(
            session=session,
            workflow_id=WORKFLOW_ID,
            task_id=TASK_ID,
            caller_agent_id=AGENT_ID,
            reason="Redirect without progress.",
            new_input={"prompt": "new"},
            inherit_progress=False,
        )

        new_task = session.add.call_args_list[0][0][0]
        assert "progress" not in new_task.metadata_
        assert "progress_message" not in new_task.metadata_
        assert new_task.metadata_["redirect_reason"] == "Redirect without progress."


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRedirectRequestValidation:
    """Request body validation."""

    @pytest.mark.asyncio
    async def test_missing_reason_returns_422(self) -> None:
        """POST redirect without reason field returns 422."""
        app = _create_test_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                _url(),
                json={"new_input": {"prompt": "test"}},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_new_input_returns_422(self) -> None:
        """POST redirect without new_input field returns 422."""
        app = _create_test_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                _url(),
                json={"reason": "need to change"},
            )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_body_returns_422(self) -> None:
        """POST redirect with empty body returns 422."""
        app = _create_test_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_url(), json={})

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Auth required (401)
# ---------------------------------------------------------------------------


class TestRedirectAuthRequired:
    """Auth is required for redirect endpoint."""

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self) -> None:
        """POST redirect without auth returns 401."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(_url(), json=_redirect_body())

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Response format — RFC field names
# ---------------------------------------------------------------------------


class TestRedirectResponseFormat:
    """Response uses RFC field names throughout."""

    @pytest.mark.asyncio
    async def test_uses_caller_not_principal_agent_id(self) -> None:
        """Response uses 'caller' not 'principal_agent_id'."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        assert "caller" in data
        assert "principal_agent_id" not in data

    @pytest.mark.asyncio
    async def test_uses_executor_not_executor_agent_id(self) -> None:
        """Response uses 'executor' not 'executor_agent_id'."""
        app = _create_test_app()
        new_task = _make_new_task()
        original_task = _make_original_task()

        with (
            patch(
                "fleet_api.tasks.routes.redirect_task",
                new_callable=AsyncMock,
                return_value=(new_task, original_task),
            ),
            patch(
                "fleet_api.tasks.routes.build_lineage_chain",
                new_callable=AsyncMock,
                return_value=[TASK_ID, NEW_TASK_ID],
            ),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(_url(), json=_redirect_body())

        data = response.json()
        assert "executor" in data
        assert "executor_agent_id" not in data
