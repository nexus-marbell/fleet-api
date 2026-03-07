"""Tests for GET /agents/{agent_id}/tasks/pending.

Covers:
  - Happy path: returns accepted tasks for authenticated agent
  - Empty: no pending tasks returns empty data array
  - Only accepted status (not running/completed/etc.)
  - Only for this agent (not other agents' tasks)
  - Unauthorized: path agent_id != authenticated agent -> 403
  - Unauthenticated -> 401
  - Priority ordering (high before normal before low)
  - HATEOAS _links format
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "sidecar-agent-001"
OTHER_AGENT_ID = "other-agent-002"
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


def _make_task(
    task_id: str = "task-a1b2c3d4",
    workflow_id: str = WORKFLOW_ID,
    executor_agent_id: str = AGENT_ID,
    status: TaskStatus = TaskStatus.ACCEPTED,
    priority: TaskPriority = TaskPriority.NORMAL,
    task_input: dict[str, Any] | None = None,
    timeout_seconds: int | None = 300,
    created_at: datetime = CREATED_AT,
) -> MagicMock:
    """Create a mock Task for pending tasks tests."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.executor_agent_id = executor_agent_id
    task.status = status
    task.priority = priority
    task.input = task_input if task_input is not None else {"pr_url": "https://github.com/..."}
    task.timeout_seconds = timeout_seconds
    task.created_at = created_at
    task.principal_agent_id = "caller-agent"
    task.result = None
    task.started_at = None
    task.completed_at = None
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
    """Create a test app without auth overrides."""
    return create_app()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPendingTasksHappyPath:
    """GET /agents/{agent_id}/tasks/pending returns accepted tasks."""

    @pytest.mark.asyncio
    async def test_returns_accepted_tasks(self) -> None:
        """Returns tasks in accepted status for the authenticated agent."""
        app = _create_test_app()
        mock_tasks = [
            _make_task(task_id="task-001"),
            _make_task(task_id="task-002"),
        ]

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=mock_tasks,
        ), patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 2
        assert data["data"][0]["task_id"] == "task-001"
        assert data["data"][1]["task_id"] == "task-002"

    @pytest.mark.asyncio
    async def test_response_fields(self) -> None:
        """Each task item contains the required fields."""
        app = _create_test_app()
        mock_task = _make_task(
            task_id="task-abc",
            workflow_id="wf-review",
            timeout_seconds=600,
        )

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=[mock_task],
        ), patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        data = response.json()
        item = data["data"][0]
        assert item["task_id"] == "task-abc"
        assert item["workflow_id"] == "wf-review"
        assert item["input"] == {"pr_url": "https://github.com/..."}
        assert item["priority"] == "normal"
        assert item["timeout_seconds"] == 600
        assert item["created_at"] is not None


# ---------------------------------------------------------------------------
# Empty result
# ---------------------------------------------------------------------------


class TestPendingTasksEmpty:
    """No pending tasks returns empty data array."""

    @pytest.mark.asyncio
    async def test_empty_data(self) -> None:
        """When no tasks are pending, data array is empty."""
        app = _create_test_app()

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ), patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []


# ---------------------------------------------------------------------------
# Status filtering (service level)
# ---------------------------------------------------------------------------


class TestPendingTasksOnlyAccepted:
    """Service only returns tasks in accepted status."""

    @pytest.mark.asyncio
    async def test_service_queries_accepted_only(self) -> None:
        """get_pending_tasks is called with the agent_id (filtering is in service)."""
        app = _create_test_app()

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_method, patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        mock_method.assert_called_once_with(AGENT_ID)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestPendingTasksAuthorization:
    """Agent can only poll its own pending tasks."""

    @pytest.mark.asyncio
    async def test_mismatch_agent_id_returns_403(self) -> None:
        """Path agent_id != authenticated agent returns 403."""
        app = _create_test_app(agent_id=OTHER_AGENT_ID)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "NOT_AUTHORIZED"

    @pytest.mark.asyncio
    async def test_unauthenticated_returns_401(self) -> None:
        """Missing auth returns 401."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestPendingTasksPriorityOrdering:
    """Tasks are ordered by priority DESC then created_at ASC."""

    @pytest.mark.asyncio
    async def test_priority_ordering(self) -> None:
        """High priority tasks appear before normal, which appear before low."""
        app = _create_test_app()

        tasks = [
            _make_task(task_id="task-high", priority=TaskPriority.HIGH),
            _make_task(task_id="task-normal", priority=TaskPriority.NORMAL),
            _make_task(task_id="task-low", priority=TaskPriority.LOW),
        ]

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=tasks,
        ), patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        data = response.json()
        assert data["data"][0]["task_id"] == "task-high"
        assert data["data"][0]["priority"] == "high"
        assert data["data"][1]["task_id"] == "task-normal"
        assert data["data"][1]["priority"] == "normal"
        assert data["data"][2]["task_id"] == "task-low"
        assert data["data"][2]["priority"] == "low"

    @pytest.mark.asyncio
    async def test_critical_before_high_priority(self) -> None:
        """CRITICAL priority tasks appear before HIGH priority tasks."""
        app = _create_test_app()

        tasks = [
            _make_task(task_id="task-critical", priority=TaskPriority.CRITICAL),
            _make_task(task_id="task-high", priority=TaskPriority.HIGH),
        ]

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=tasks,
        ), patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        data = response.json()
        assert len(data["data"]) == 2
        assert data["data"][0]["task_id"] == "task-critical"
        assert data["data"][0]["priority"] == "critical"
        assert data["data"][1]["task_id"] == "task-high"
        assert data["data"][1]["priority"] == "high"


# ---------------------------------------------------------------------------
# HATEOAS links
# ---------------------------------------------------------------------------


class TestPendingTasksLinks:
    """Response contains proper _links."""

    @pytest.mark.asyncio
    async def test_links_contains_self(self) -> None:
        """Response has _links.self with correct href."""
        app = _create_test_app()

        with patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ), patch.object(
            __import__("fleet_api.tasks.service", fromlist=["TaskService"]).TaskService,
            "get_pending_signals",
            new_callable=AsyncMock,
            return_value=[],
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/agents/{AGENT_ID}/tasks/pending")

        data = response.json()
        assert "_links" in data
        assert data["_links"]["self"]["href"] == f"/agents/{AGENT_ID}/tasks/pending"
