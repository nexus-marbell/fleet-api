"""Tests for task read/list endpoints (Issue #15).

Uses FastAPI dependency overrides for auth and task service so tests
have no real database dependency. Tests cover:
  GET /workflows/{workflow_id}/tasks/{task_id}
  GET /workflows/{workflow_id}/tasks
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import ErrorCode, NotFoundError
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus
from fleet_api.tasks.routes import get_task_service
from fleet_api.tasks.service import build_task_links, encode_task_cursor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "nexus-marbell"
WORKFLOW_ID = "wf-cellular-automaton"

# Links that appear unconditionally for every task status
UNIVERSAL_LINKS = {"self", "workflow", "stream"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    task_id: str = "task-a1b2c3d4",
    workflow_id: str = WORKFLOW_ID,
    principal_agent_id: str = AGENT_ID,
    executor_agent_id: str | None = "grok-pi-dev",
    status: TaskStatus = TaskStatus.COMPLETED,
    input_data: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    priority: TaskPriority = TaskPriority.NORMAL,
    created_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Task object."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.principal_agent_id = principal_agent_id
    task.executor_agent_id = executor_agent_id
    task.status = status
    task.input = input_data or {"prompt": "test"}
    task.result = result
    task.priority = priority
    task.created_at = created_at or datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
    task.started_at = started_at
    task.completed_at = completed_at
    task.metadata_ = metadata
    return task


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _create_test_app(mock_service: MagicMock, agent_id: str = AGENT_ID) -> Any:
    """Create a test app with auth and service overrides."""
    app = create_app()

    async def mock_auth() -> AuthenticatedAgent:
        mock_key = MagicMock(spec=Ed25519PublicKey)
        return AuthenticatedAgent(agent_id=agent_id, public_key=mock_key)

    app.dependency_overrides[require_auth] = mock_auth
    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    app.dependency_overrides[get_task_service] = lambda: mock_service
    return app


def _create_unauthed_app(mock_service: MagicMock) -> Any:
    """Create a test app WITHOUT auth override (requires real auth)."""
    app = create_app()
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
def app(mock_service: MagicMock) -> Any:
    return _create_test_app(mock_service)


@pytest.fixture
async def client(app: Any) -> AsyncClient:  # type: ignore[misc]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /workflows/{workflow_id}/tasks/{task_id}
# ---------------------------------------------------------------------------


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_completed_task(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task in completed state includes result, quality, warnings, duration_seconds."""
        task = _make_task(
            status=TaskStatus.COMPLETED,
            result={"output": "done"},
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
            metadata={
                "warnings": [],
                "quality": {
                    "input_valid": True,
                    "execution_clean": True,
                    "result_complete": True,
                },
            },
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["task_id"] == "task-a1b2c3d4"
        assert data["workflow_id"] == WORKFLOW_ID
        assert data["status"] == "completed"
        assert data["caller"] == AGENT_ID
        assert data["executor"] == "grok-pi-dev"
        assert data["priority"] == "normal"
        assert data["input"] == {"prompt": "test"}
        assert data["result"] == {"output": "done"}
        assert data["warnings"] == []
        assert data["quality"]["input_valid"] is True
        assert data["quality"]["execution_clean"] is True
        assert data["quality"]["result_complete"] is True
        assert data["duration_seconds"] == 13
        assert data["started_at"] is not None
        assert data["completed_at"] is not None
        assert "_links" in data

    @pytest.mark.asyncio
    async def test_get_running_task(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task in running state includes progress, no result."""
        task = _make_task(
            status=TaskStatus.RUNNING,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            metadata={"progress": 42, "estimated_completion": "2026-03-07T14:35:00Z"},
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"
        assert data["progress"] == 42
        assert data["estimated_completion"] == "2026-03-07T14:35:00Z"
        assert "result" not in data

    @pytest.mark.asyncio
    async def test_get_accepted_task(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task in accepted state has minimal fields."""
        task = _make_task(status=TaskStatus.ACCEPTED)
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert data["task_id"] == "task-a1b2c3d4"
        assert "result" not in data
        assert "progress" not in data

    @pytest.mark.asyncio
    async def test_get_failed_task_includes_result(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task in failed state includes result (error info) and completed_at."""
        task = _make_task(
            status=TaskStatus.FAILED,
            result={"error": "timeout"},
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
            metadata={"warnings": ["partial result discarded"]},
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"
        assert data["result"] == {"error": "timeout"}
        assert data["completed_at"] is not None
        assert data["duration_seconds"] == 13
        assert data["warnings"] == ["partial result discarded"]

    @pytest.mark.asyncio
    async def test_get_paused_task(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task in paused state has started_at but no result."""
        task = _make_task(
            status=TaskStatus.PAUSED,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "paused"
        assert "started_at" in data
        assert "result" not in data

    @pytest.mark.asyncio
    async def test_task_not_found(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET nonexistent task returns 404 TASK_NOT_FOUND."""
        mock_service.get_task = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message="Task 'task-ghost' not found in workflow 'wf-cellular-automaton'.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-ghost"
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_workflow_not_found(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task on nonexistent workflow returns 404 WORKFLOW_NOT_FOUND."""
        mock_service.get_task = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/workflows/wf-ghost/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_wrong_workflow_for_task(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET task that exists but on different workflow returns 404 TASK_NOT_FOUND."""
        mock_service.get_task = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.TASK_NOT_FOUND,
                message="Task 'task-a1b2c3d4' not found in workflow 'wf-other'.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/workflows/wf-other/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# State-dependent HATEOAS links
# ---------------------------------------------------------------------------


class TestHATEOASLinks:
    @pytest.mark.asyncio
    async def test_accepted_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Accepted tasks have cancel link only (plus self + workflow + stream)."""
        task = _make_task(status=TaskStatus.ACCEPTED)
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert links["self"]["href"].endswith("/tasks/task-a1b2c3d4")
        assert "stream" in links
        assert links["stream"]["href"].endswith("/stream")
        assert "cancel" in links
        assert links["cancel"]["method"] == "POST"
        assert links["cancel"]["href"].endswith("/cancel")
        assert "workflow" in links
        # Should NOT have pause, resume, retask, rerun
        assert "pause" not in links
        assert "resume" not in links
        assert "retask" not in links
        assert "rerun" not in links

    @pytest.mark.asyncio
    async def test_running_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Running tasks have cancel, pause, context, redirect links with method: POST."""
        task = _make_task(
            status=TaskStatus.RUNNING,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "stream" in links
        assert "cancel" in links
        assert links["cancel"]["href"].endswith("/cancel")
        assert links["cancel"]["method"] == "POST"
        assert "pause" in links
        assert links["pause"]["href"].endswith("/pause")
        assert links["pause"]["method"] == "POST"
        assert "context" in links
        assert links["context"]["href"].endswith("/context")
        assert links["context"]["method"] == "POST"
        assert "redirect" in links
        assert links["redirect"]["href"].endswith("/redirect")
        assert links["redirect"]["method"] == "POST"
        assert "workflow" in links

    @pytest.mark.asyncio
    async def test_paused_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Paused tasks: resume, cancel, context, redirect, plus self + workflow + stream."""
        task = _make_task(
            status=TaskStatus.PAUSED,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "stream" in links
        assert "resume" in links
        assert links["resume"]["href"].endswith("/resume")
        assert links["resume"]["method"] == "POST"
        assert "cancel" in links
        assert links["cancel"]["method"] == "POST"
        assert "context" in links
        assert links["context"]["href"].endswith("/context")
        assert links["context"]["method"] == "POST"
        assert "redirect" in links
        assert links["redirect"]["href"].endswith("/redirect")
        assert links["redirect"]["method"] == "POST"
        assert "workflow" in links
        # Should NOT have pause (already paused)
        assert "pause" not in links

    @pytest.mark.asyncio
    async def test_completed_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Completed tasks have retask, rerun links with method: POST."""
        task = _make_task(
            status=TaskStatus.COMPLETED,
            result={"output": "done"},
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "stream" in links
        assert "retask" in links
        assert links["retask"]["href"].endswith("/retask")
        assert links["retask"]["method"] == "POST"
        assert "rerun" in links
        assert links["rerun"]["href"] == f"/workflows/{WORKFLOW_ID}/run"
        assert links["rerun"]["method"] == "POST"
        assert "workflow" in links

    @pytest.mark.asyncio
    async def test_failed_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Failed tasks have retask, rerun links with method: POST."""
        task = _make_task(
            status=TaskStatus.FAILED,
            result={"error": "timeout"},
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "stream" in links
        assert "retask" in links
        assert links["retask"]["method"] == "POST"
        assert "rerun" in links
        assert links["rerun"]["method"] == "POST"
        assert "workflow" in links

    @pytest.mark.asyncio
    async def test_cancelled_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Cancelled tasks have rerun link (plus self + workflow + stream)."""
        task = _make_task(
            status=TaskStatus.CANCELLED,
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "workflow" in links
        assert "stream" in links
        assert "rerun" in links
        assert links["rerun"]["method"] == "POST"
        # No retask or cancel for cancelled
        assert "retask" not in links
        assert "cancel" not in links

    @pytest.mark.asyncio
    async def test_redirected_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Redirected tasks have only self + workflow + stream links (no action links)."""
        task = _make_task(
            status=TaskStatus.REDIRECTED,
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "workflow" in links
        assert "stream" in links
        # No action links for redirected
        assert set(links.keys()) == UNIVERSAL_LINKS

    @pytest.mark.asyncio
    async def test_retasked_links(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Retasked tasks have only self + workflow + stream links (no action links)."""
        task = _make_task(
            status=TaskStatus.RETASKED,
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        assert "self" in links
        assert "workflow" in links
        assert "stream" in links
        # No action links for retasked
        assert set(links.keys()) == UNIVERSAL_LINKS

    @pytest.mark.asyncio
    async def test_all_links_use_href_format(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """All HATEOAS links use {"href": "..."} object format."""
        task = _make_task(
            status=TaskStatus.RUNNING,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        for link_name, link_value in links.items():
            assert isinstance(link_value, dict), (
                f"Link '{link_name}' should be a dict, got {type(link_value)}"
            )
            assert "href" in link_value, f"Link '{link_name}' missing 'href' key"

    @pytest.mark.asyncio
    async def test_action_links_have_method_post(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """All action links (non-self, non-workflow, non-stream) include method: POST."""
        task = _make_task(
            status=TaskStatus.RUNNING,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
        )
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        links = response.json()["_links"]
        for link_name, link_value in links.items():
            if link_name in UNIVERSAL_LINKS:
                # self, workflow, stream should NOT have method
                assert "method" not in link_value, (
                    f"Non-action link '{link_name}' should not have 'method'"
                )
            else:
                # Action links MUST have method: POST
                assert link_value.get("method") == "POST", (
                    f"Action link '{link_name}' should have method: POST"
                )


# ---------------------------------------------------------------------------
# GET /workflows/{workflow_id}/tasks (list)
# ---------------------------------------------------------------------------


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_tasks_no_filters(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list returns data array with nested pagination object."""
        task = _make_task()
        mock_service.list_tasks = AsyncMock(return_value=([task], None, False, 1))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["task_id"] == "task-a1b2c3d4"
        assert data["data"][0]["status"] == "completed"
        assert data["data"][0]["caller"] == AGENT_ID
        # Pagination is nested
        assert data["pagination"]["has_more"] is False
        assert data["pagination"]["total_count"] == 1
        assert data["pagination"]["limit"] == 20
        assert data["pagination"]["next_cursor"] is None
        assert "_links" in data
        assert "workflow" in data["_links"]

    @pytest.mark.asyncio
    async def test_list_tasks_empty(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list with no tasks returns empty data array."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        assert response.status_code == 200
        data = response.json()
        assert data["data"] == []
        assert data["pagination"]["total_count"] == 0

    @pytest.mark.asyncio
    async def test_list_tasks_filtered_by_status(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list filtered by status passes filter to service."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks?status=running"
            )

        assert response.status_code == 200
        mock_service.list_tasks.assert_called_once_with(
            workflow_id=WORKFLOW_ID,
            status="running",
            priority=None,
            caller=None,
            since=None,
            until=None,
            cursor=None,
            limit=20,
        )

    @pytest.mark.asyncio
    async def test_list_tasks_filtered_by_priority(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list filtered by priority passes filter to service."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks?priority=high"
            )

        assert response.status_code == 200
        mock_service.list_tasks.assert_called_once_with(
            workflow_id=WORKFLOW_ID,
            status=None,
            priority="high",
            caller=None,
            since=None,
            until=None,
            cursor=None,
            limit=20,
        )

    @pytest.mark.asyncio
    async def test_list_tasks_filtered_by_caller(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list filtered by caller passes filter to service."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks?caller=nexus-marbell"
            )

        assert response.status_code == 200
        mock_service.list_tasks.assert_called_once_with(
            workflow_id=WORKFLOW_ID,
            status=None,
            priority=None,
            caller="nexus-marbell",
            since=None,
            until=None,
            cursor=None,
            limit=20,
        )

    @pytest.mark.asyncio
    async def test_list_tasks_with_time_range(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list with since/until passes time range to service."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks"
                "?since=2026-03-07T00:00:00Z&until=2026-03-07T23:59:59Z"
            )

        assert response.status_code == 200
        mock_service.list_tasks.assert_called_once_with(
            workflow_id=WORKFLOW_ID,
            status=None,
            priority=None,
            caller=None,
            since="2026-03-07T00:00:00Z",
            until="2026-03-07T23:59:59Z",
            cursor=None,
            limit=20,
        )

    @pytest.mark.asyncio
    async def test_cursor_pagination(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list with cursor pagination works correctly."""
        task1 = _make_task(task_id="task-page1")
        cursor = encode_task_cursor(
            "task-page1", datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
        )
        mock_service.list_tasks = AsyncMock(
            return_value=([task1], cursor, True, 5)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks?limit=1"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["has_more"] is True
        assert data["pagination"]["next_cursor"] is not None
        assert data["pagination"]["total_count"] == 5
        assert data["pagination"]["limit"] == 1
        assert "next" in data["_links"]
        assert "href" in data["_links"]["next"]

    @pytest.mark.asyncio
    async def test_cursor_page_two(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list using cursor from page 1 returns page 2."""
        cursor = encode_task_cursor(
            "task-page1", datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
        )
        task2 = _make_task(task_id="task-page2")
        mock_service.list_tasks = AsyncMock(
            return_value=([task2], None, False, 5)
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks?cursor={cursor}&limit=1"
            )

        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["task_id"] == "task-page2"
        assert data["pagination"]["has_more"] is False

    @pytest.mark.asyncio
    async def test_total_count_in_response(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Response includes total_count in pagination object."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 42))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        data = response.json()
        assert data["pagination"]["total_count"] == 42

    @pytest.mark.asyncio
    async def test_custom_limit(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Custom limit is reflected in pagination object."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks?limit=50"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["limit"] == 50

    @pytest.mark.asyncio
    async def test_list_workflow_not_found(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """GET tasks list on nonexistent workflow returns 404."""
        mock_service.list_tasks = AsyncMock(
            side_effect=NotFoundError(
                code=ErrorCode.WORKFLOW_NOT_FOUND,
                message="Workflow 'wf-ghost' not found.",
            )
        )

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflows/wf-ghost/tasks")

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_list_links_use_href_format(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """List response links use {"href": "..."} object format."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        links = response.json()["_links"]
        for link_name, link_value in links.items():
            assert isinstance(link_value, dict), (
                f"Link '{link_name}' should be a dict, got {type(link_value)}"
            )
            assert "href" in link_value, f"Link '{link_name}' missing 'href' key"

    @pytest.mark.asyncio
    async def test_no_next_link_when_no_more_pages(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """List response has no next link when has_more is False."""
        mock_service.list_tasks = AsyncMock(return_value=([], None, False, 0))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        links = response.json()["_links"]
        assert "next" not in links

    @pytest.mark.asyncio
    async def test_summary_items_have_stream_link(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Each summary item in the list response includes a stream link."""
        task = _make_task()
        mock_service.list_tasks = AsyncMock(return_value=([task], None, False, 1))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        item = response.json()["data"][0]
        assert "stream" in item["_links"]
        assert item["_links"]["stream"]["href"].endswith("/stream")


# ---------------------------------------------------------------------------
# Response field name compliance
# ---------------------------------------------------------------------------


class TestResponseFieldNames:
    @pytest.mark.asyncio
    async def test_detail_uses_caller_not_principal_agent_id(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Detail response uses 'caller' not 'principal_agent_id'."""
        task = _make_task(status=TaskStatus.ACCEPTED)
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        data = response.json()
        assert "caller" in data
        assert "principal_agent_id" not in data

    @pytest.mark.asyncio
    async def test_detail_uses_executor_not_executor_agent_id(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """Detail response uses 'executor' not 'executor_agent_id'."""
        task = _make_task(status=TaskStatus.ACCEPTED)
        mock_service.get_task = AsyncMock(return_value=task)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        data = response.json()
        assert "executor" in data
        assert "executor_agent_id" not in data

    @pytest.mark.asyncio
    async def test_list_uses_caller_not_principal_agent_id(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """List summary response uses 'caller' not 'principal_agent_id'."""
        task = _make_task()
        mock_service.list_tasks = AsyncMock(return_value=([task], None, False, 1))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        item = response.json()["data"][0]
        assert "caller" in item
        assert "principal_agent_id" not in item

    @pytest.mark.asyncio
    async def test_list_summary_excludes_input_and_result(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """List summary items exclude input and full result payload."""
        task = _make_task(
            status=TaskStatus.COMPLETED,
            result={"output": "big payload"},
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.list_tasks = AsyncMock(return_value=([task], None, False, 1))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        item = response.json()["data"][0]
        assert "input" not in item
        assert "result" not in item

    @pytest.mark.asyncio
    async def test_list_summary_includes_duration_for_completed(
        self, app: Any, mock_service: MagicMock
    ) -> None:
        """List summary includes duration_seconds for completed tasks."""
        task = _make_task(
            status=TaskStatus.COMPLETED,
            started_at=datetime(2026, 3, 7, 14, 30, 2, tzinfo=UTC),
            completed_at=datetime(2026, 3, 7, 14, 30, 15, tzinfo=UTC),
        )
        mock_service.list_tasks = AsyncMock(return_value=([task], None, False, 1))

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        item = response.json()["data"][0]
        assert item["duration_seconds"] == 13


# ---------------------------------------------------------------------------
# Auth requirement
# ---------------------------------------------------------------------------


class TestAuthRequired:
    @pytest.mark.asyncio
    async def test_get_task_requires_auth(
        self, mock_service: MagicMock
    ) -> None:
        """GET task without auth returns 401."""
        app = _create_unauthed_app(mock_service)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-a1b2c3d4"
            )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_list_tasks_requires_auth(
        self, mock_service: MagicMock
    ) -> None:
        """GET task list without auth returns 401."""
        app = _create_unauthed_app(mock_service)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"/workflows/{WORKFLOW_ID}/tasks")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# build_task_links unit tests
# ---------------------------------------------------------------------------


class TestBuildTaskLinks:
    def test_all_statuses_have_self_workflow_stream(self) -> None:
        """Every task status produces self, workflow, and stream links."""
        for status in TaskStatus:
            links = build_task_links("task-1", "wf-1", status)
            assert "self" in links, f"Missing 'self' for {status.value}"
            assert "workflow" in links, f"Missing 'workflow' for {status.value}"
            assert "stream" in links, f"Missing 'stream' for {status.value}"

    def test_link_paths_use_correct_format(self) -> None:
        """Links use /workflows/{id}/tasks/{id} format with href objects."""
        links = build_task_links("task-abc", "wf-xyz", TaskStatus.RUNNING)
        assert links["self"]["href"] == "/workflows/wf-xyz/tasks/task-abc"
        assert links["pause"]["href"] == "/workflows/wf-xyz/tasks/task-abc/pause"
        assert links["workflow"]["href"] == "/workflows/wf-xyz"
        assert links["stream"]["href"] == "/workflows/wf-xyz/tasks/task-abc/stream"

    def test_all_links_are_href_objects(self) -> None:
        """Every link value is a dict with an 'href' key."""
        for status in TaskStatus:
            links = build_task_links("task-1", "wf-1", status)
            for link_name, link_value in links.items():
                assert isinstance(link_value, dict), (
                    f"Link '{link_name}' for {status.value} should be dict"
                )
                assert "href" in link_value, (
                    f"Link '{link_name}' for {status.value} missing 'href'"
                )

    def test_action_links_have_method_post(self) -> None:
        """All action links include method: POST."""
        links = build_task_links("task-1", "wf-1", TaskStatus.RUNNING)
        for action in ("cancel", "pause", "context", "redirect"):
            assert links[action]["method"] == "POST", (
                f"Action link '{action}' should have method: POST"
            )

    def test_non_action_links_have_no_method(self) -> None:
        """self, workflow, stream links do not include a method field."""
        links = build_task_links("task-1", "wf-1", TaskStatus.RUNNING)
        for link_name in ("self", "workflow", "stream"):
            assert "method" not in links[link_name], (
                f"Non-action link '{link_name}' should not have 'method'"
            )

    def test_rerun_points_to_workflow_run(self) -> None:
        """Rerun link points to /workflows/{id}/run with method: POST."""
        links = build_task_links("task-1", "wf-1", TaskStatus.COMPLETED)
        assert links["rerun"]["href"] == "/workflows/wf-1/run"
        assert links["rerun"]["method"] == "POST"

    def test_accepted_action_links(self) -> None:
        """Accepted status has exactly: cancel."""
        links = build_task_links("task-1", "wf-1", TaskStatus.ACCEPTED)
        action_links = set(links.keys()) - UNIVERSAL_LINKS
        assert action_links == {"cancel"}

    def test_running_action_links(self) -> None:
        """Running status has exactly: cancel, pause, context, redirect."""
        links = build_task_links("task-1", "wf-1", TaskStatus.RUNNING)
        action_links = set(links.keys()) - UNIVERSAL_LINKS
        assert action_links == {"cancel", "pause", "context", "redirect"}

    def test_paused_action_links(self) -> None:
        """Paused status has exactly: resume, cancel, context, redirect."""
        links = build_task_links("task-1", "wf-1", TaskStatus.PAUSED)
        action_links = set(links.keys()) - UNIVERSAL_LINKS
        assert action_links == {"resume", "cancel", "context", "redirect"}

    def test_paused_resume_path(self) -> None:
        """Paused resume link points to /tasks/{id}/resume, not /tasks/{id}."""
        links = build_task_links("task-1", "wf-1", TaskStatus.PAUSED)
        assert links["resume"]["href"] == "/workflows/wf-1/tasks/task-1/resume"

    def test_completed_action_links(self) -> None:
        """Completed status has exactly: retask, rerun."""
        links = build_task_links("task-1", "wf-1", TaskStatus.COMPLETED)
        action_links = set(links.keys()) - UNIVERSAL_LINKS
        assert action_links == {"retask", "rerun"}

    def test_failed_action_links(self) -> None:
        """Failed status has exactly: retask, rerun."""
        links = build_task_links("task-1", "wf-1", TaskStatus.FAILED)
        action_links = set(links.keys()) - UNIVERSAL_LINKS
        assert action_links == {"retask", "rerun"}

    def test_cancelled_action_links(self) -> None:
        """Cancelled status has exactly: rerun."""
        links = build_task_links("task-1", "wf-1", TaskStatus.CANCELLED)
        action_links = set(links.keys()) - UNIVERSAL_LINKS
        assert action_links == {"rerun"}

    def test_terminal_no_action_states_have_only_universal_links(self) -> None:
        """Retasked, redirected have only self + workflow + stream (no action links)."""
        for status in (TaskStatus.RETASKED, TaskStatus.REDIRECTED):
            links = build_task_links("task-1", "wf-1", status)
            assert set(links.keys()) == UNIVERSAL_LINKS, (
                f"{status.value} should only have {UNIVERSAL_LINKS}, got {set(links.keys())}"
            )
