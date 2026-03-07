"""Tests for SSE task streaming (Phase 2 Unit 1).

Tests cover:
  - SSE format output (format_sse_event helper)
  - Streaming existing events (create task + events, then stream)
  - Last-Event-Id reconnection (create events, stream from middle)
  - Heartbeat keepalive timing
  - Terminal event closes stream
  - Auth required
  - Task not found returns 404
  - Workflow not found returns 404
  - New event types (context_injected, escalation) can be posted and streamed
  - Stream closes on each terminal state type
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import ErrorCode, NotFoundError
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.models import Task, TaskEvent, TaskPriority, TaskStatus
from fleet_api.tasks.sse import format_sse_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_ID = "test-agent-001"
WORKFLOW_ID = "wf-streaming-test"
TASK_ID = "task-sse-0001"
CREATED_AT = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for auth override."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


def _make_task_event(
    task_id: str = TASK_ID,
    event_type: str = "log",
    data: dict[str, Any] | None = None,
    sequence: int = 1,
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock TaskEvent."""
    event = MagicMock(spec=TaskEvent)
    event.task_id = task_id
    event.event_type = event_type
    event.data = data if data is not None else {"message": f"event {sequence}"}
    event.sequence = sequence
    event.created_at = created_at or CREATED_AT
    return event


def _make_task(
    task_id: str = TASK_ID,
    workflow_id: str = WORKFLOW_ID,
    status: TaskStatus = TaskStatus.RUNNING,
) -> MagicMock:
    """Create a mock Task."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.status = status
    task.principal_agent_id = AGENT_ID
    task.executor_agent_id = "executor-001"
    task.priority = TaskPriority.NORMAL
    task.created_at = CREATED_AT
    task.input = {"prompt": "test"}
    return task


def _make_workflow(workflow_id: str = WORKFLOW_ID) -> MagicMock:
    """Create a mock Workflow."""
    wf = MagicMock()
    wf.id = workflow_id
    wf.owner_agent_id = AGENT_ID
    return wf


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


def _parse_sse_events(raw: str) -> list[dict[str, str]]:
    """Parse raw SSE text into a list of event dicts with id, event, data fields."""
    events = []
    current: dict[str, str] = {}
    for line in raw.split("\n"):
        if line == "":
            if current:
                events.append(current)
                current = {}
        elif line.startswith("id: "):
            current["id"] = line[4:]
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = line[6:]
    if current:
        events.append(current)
    return events


# ---------------------------------------------------------------------------
# Test: format_sse_event helper
# ---------------------------------------------------------------------------


class TestFormatSSEEvent:
    """Tests for the format_sse_event helper function."""

    def test_basic_format(self) -> None:
        """SSE event has id, event, data lines with trailing double newline."""
        result = format_sse_event("log", {"message": "hello"}, 1)
        assert result == 'id: 1\nevent: log\ndata: {"message": "hello"}\n\n'

    def test_sequence_as_id(self) -> None:
        """The sequence number is used as the SSE id field."""
        result = format_sse_event("progress", {"progress": 50}, 42)
        lines = result.strip().split("\n")
        assert lines[0] == "id: 42"

    def test_event_type_in_event_field(self) -> None:
        """The event_type is used as the SSE event field."""
        result = format_sse_event("status", {"status": "running"}, 3)
        lines = result.strip().split("\n")
        assert lines[1] == "event: status"

    def test_data_is_json(self) -> None:
        """The data field is valid JSON."""
        result = format_sse_event("log", {"key": "value", "num": 123}, 5)
        lines = result.strip().split("\n")
        data_line = lines[2]
        assert data_line.startswith("data: ")
        parsed = json.loads(data_line[6:])
        assert parsed == {"key": "value", "num": 123}

    def test_empty_data(self) -> None:
        """Empty data dict is serialized as {}."""
        result = format_sse_event("heartbeat", {}, 10)
        assert "data: {}" in result

    def test_trailing_double_newline(self) -> None:
        """SSE spec requires each event to end with two newlines."""
        result = format_sse_event("log", {}, 1)
        assert result.endswith("\n\n")

    def test_datetime_serialization(self) -> None:
        """Datetime values in data are serialized via default=str."""
        dt = datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC)
        result = format_sse_event("log", {"timestamp": dt}, 1)
        assert "2026-03-07" in result

    def test_new_event_type_context_injected(self) -> None:
        """context_injected event type formats correctly."""
        result = format_sse_event("context_injected", {"context_id": "ctx-1"}, 7)
        assert "event: context_injected" in result

    def test_new_event_type_escalation(self) -> None:
        """escalation event type formats correctly."""
        result = format_sse_event("escalation", {"reason": "blocked"}, 8)
        assert "event: escalation" in result


# ---------------------------------------------------------------------------
# Test: SSE stream endpoint — existing events
# ---------------------------------------------------------------------------


class TestStreamExistingEvents:
    """Streaming replays existing events from the database."""

    @pytest.mark.asyncio
    async def test_stream_replays_existing_events(self) -> None:
        """GET stream returns all existing events in SSE format, closes on terminal."""
        app = _create_test_app()
        mock_workflow = _make_workflow()
        mock_task = _make_task(status=TaskStatus.COMPLETED)
        events = [
            _make_task_event(event_type="log", data={"message": "starting"}, sequence=1),
            _make_task_event(event_type="progress", data={"progress": 50}, sequence=2),
            _make_task_event(
                event_type="completed",
                data={"result": {"output": "done"}},
                sequence=3,
            ),
        ]

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=lambda model, id_: {
            "Workflow": mock_workflow,
            "Task": mock_task,
        }.get(model.__name__, None) if hasattr(model, '__name__') else None)

        # Mock the query for events
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = events
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

        parsed = _parse_sse_events(response.text)
        assert len(parsed) == 3
        assert parsed[0]["event"] == "log"
        assert parsed[0]["id"] == "1"
        assert parsed[1]["event"] == "progress"
        assert parsed[2]["event"] == "completed"

    @pytest.mark.asyncio
    async def test_stream_empty_task_with_terminal_status(self) -> None:
        """Stream closes immediately if task is already in terminal state with no events."""
        app = _create_test_app()
        mock_workflow = _make_workflow()
        mock_task = _make_task(status=TaskStatus.COMPLETED)

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                return mock_task
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        # No events found
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 200
        # No events emitted, stream closes immediately
        assert response.text == "" or len(_parse_sse_events(response.text)) == 0


# ---------------------------------------------------------------------------
# Test: Last-Event-Id reconnection
# ---------------------------------------------------------------------------


class TestLastEventIdReconnection:
    """Last-Event-Id header enables replay from a specific sequence."""

    @pytest.mark.asyncio
    async def test_reconnection_replays_from_sequence(self) -> None:
        """Last-Event-Id: 2 only replays events with sequence > 2."""
        app = _create_test_app()
        mock_workflow = _make_workflow()
        mock_task = _make_task(status=TaskStatus.COMPLETED)

        # Only events after sequence 2
        events = [
            _make_task_event(event_type="progress", data={"progress": 75}, sequence=3),
            _make_task_event(
                event_type="completed",
                data={"result": {"output": "done"}},
                sequence=4,
            ),
        ]

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                return mock_task
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = events
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
                headers={"Last-Event-Id": "2"},
            )

        assert response.status_code == 200
        parsed = _parse_sse_events(response.text)
        assert len(parsed) == 2
        assert parsed[0]["id"] == "3"
        assert parsed[1]["id"] == "4"
        assert parsed[1]["event"] == "completed"


# ---------------------------------------------------------------------------
# Test: Terminal events close stream
# ---------------------------------------------------------------------------


class TestTerminalEventClosesStream:
    """Stream closes after emitting a terminal event."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal_event_type,terminal_data",
        [
            ("completed", {"result": {"output": "done"}}),
            ("failed", {"error_code": "EXECUTION_FAILED", "message": "boom"}),
            ("status", {"status": "cancelled", "reason": "user cancelled"}),
            ("status", {"status": "redirected", "to_agent": "agent-b"}),
            ("status", {"status": "retasked", "new_task_id": "task-new"}),
        ],
    )
    async def test_terminal_event_closes_stream(
        self, terminal_event_type: str, terminal_data: dict[str, Any]
    ) -> None:
        """Stream stops after a terminal event is emitted."""
        app = _create_test_app()
        mock_workflow = _make_workflow()
        mock_task = _make_task(status=TaskStatus.RUNNING)

        events = [
            _make_task_event(event_type="log", data={"message": "working"}, sequence=1),
            _make_task_event(
                event_type=terminal_event_type,
                data=terminal_data,
                sequence=2,
            ),
        ]

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                return mock_task
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = events
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 200
        parsed = _parse_sse_events(response.text)
        assert len(parsed) == 2
        assert parsed[1]["event"] == terminal_event_type


# ---------------------------------------------------------------------------
# Test: Heartbeat keepalive
# ---------------------------------------------------------------------------


class TestHeartbeatKeepalive:
    """Heartbeat keepalive is sent after the configured interval."""

    @pytest.mark.asyncio
    async def test_heartbeat_sent_after_interval(self) -> None:
        """When no new events arrive, a heartbeat is emitted after the interval."""
        app = _create_test_app()
        mock_workflow = _make_workflow()

        # Task stays running for enough polls to trigger heartbeat,
        # then goes terminal after heartbeat fires.
        # With poll_interval=0.1 and heartbeat_interval=0.3, the heartbeat
        # fires after ~3 polls. We go terminal after ~6 polls.
        get_task_call_count = 0

        async def mock_get(model, id_):
            nonlocal get_task_call_count
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                get_task_call_count += 1
                # Stay running long enough for heartbeat to fire (calls 1-5)
                # Then go terminal (call 6+)
                if get_task_call_count <= 5:
                    return _make_task(status=TaskStatus.RUNNING)
                return _make_task(status=TaskStatus.COMPLETED)
            return None

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=mock_get)
        mock_session.expire_all = MagicMock()

        # All event queries return empty (no real events, just heartbeats)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        # Use very short intervals: poll every 0.1s, heartbeat every 0.3s
        with patch("fleet_api.tasks.sse.settings") as mock_settings, \
             patch("fleet_api.tasks.sse._POLL_INTERVAL", 0.1):
            mock_settings.fleet_sse_heartbeat_interval = 0.3

            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test", timeout=10.0
            ) as client:
                response = await client.get(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
                )

        assert response.status_code == 200
        parsed = _parse_sse_events(response.text)
        # Should have at least one heartbeat before terminal detection
        heartbeats = [e for e in parsed if e.get("event") == "heartbeat"]
        assert len(heartbeats) >= 1
        # Heartbeat data should indicate keepalive
        hb_data = json.loads(heartbeats[0]["data"])
        assert hb_data["type"] == "keepalive"


# ---------------------------------------------------------------------------
# Test: Auth required
# ---------------------------------------------------------------------------


class TestStreamAuthRequired:
    """SSE stream endpoint requires authentication."""

    @pytest.mark.asyncio
    async def test_auth_required(self) -> None:
        """GET stream without auth returns 401."""
        app = _create_unauthenticated_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
            )

        # Auth middleware returns 401 or 503 (depending on agent lookup config)
        assert response.status_code in (401, 503)


# ---------------------------------------------------------------------------
# Test: Not found errors
# ---------------------------------------------------------------------------


class TestStreamNotFound:
    """404 errors for missing workflow or task."""

    @pytest.mark.asyncio
    async def test_workflow_not_found(self) -> None:
        """GET stream with non-existent workflow returns 404."""
        app = _create_test_app()

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(return_value=None)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/wf-nonexistent/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "WORKFLOW_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_task_not_found(self) -> None:
        """GET stream with non-existent task returns 404."""
        app = _create_test_app()
        mock_workflow = _make_workflow()

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/task-nonexistent/stream",
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_task_wrong_workflow(self) -> None:
        """GET stream where task exists but belongs to a different workflow returns 404."""
        app = _create_test_app()
        mock_workflow = _make_workflow(workflow_id="wf-other")
        mock_task = _make_task(workflow_id="wf-different")

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                return mock_task
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/wf-other/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "TASK_NOT_FOUND"


# ---------------------------------------------------------------------------
# Test: New event types (context_injected, escalation)
# ---------------------------------------------------------------------------


class TestNewEventTypes:
    """The new context_injected and escalation event types stream correctly."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_type,event_data",
        [
            ("context_injected", {"context_id": "ctx-abc", "acknowledged": True}),
            ("escalation", {"reason": "requires principal decision", "severity": "high"}),
        ],
    )
    async def test_new_event_types_stream(
        self, event_type: str, event_data: dict[str, Any]
    ) -> None:
        """New event types are streamed correctly in SSE format."""
        app = _create_test_app()
        mock_workflow = _make_workflow()
        mock_task = _make_task(status=TaskStatus.COMPLETED)

        events = [
            _make_task_event(event_type=event_type, data=event_data, sequence=1),
            _make_task_event(
                event_type="completed",
                data={"result": {"output": "done"}},
                sequence=2,
            ),
        ]

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                return mock_task
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = events
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 200
        parsed = _parse_sse_events(response.text)
        assert len(parsed) == 2
        assert parsed[0]["event"] == event_type
        first_data = json.loads(parsed[0]["data"])
        for key, value in event_data.items():
            assert first_data[key] == value


# ---------------------------------------------------------------------------
# Test: New event types can be posted via sidecar endpoint
# ---------------------------------------------------------------------------


class TestNewEventTypesPostable:
    """The new event types can be posted via POST /tasks/{task_id}/events."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", ["context_injected", "escalation"])
    async def test_post_new_event_type(self, event_type: str) -> None:
        """POST new event types returns 201 and records the event."""
        app = _create_test_app()

        mock_event = MagicMock()
        mock_event.id = 42
        mock_event.event_type = event_type
        mock_event.sequence = 5
        mock_event.created_at = CREATED_AT

        mock_task = _make_task()

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
                        "event_type": event_type,
                        "data": {"info": "test"},
                        "sequence": 5,
                    },
                )

            assert response.status_code == 201
            data = response.json()
            assert data["received"] is True
            assert data["event_type"] == event_type


# ---------------------------------------------------------------------------
# Test: Response headers
# ---------------------------------------------------------------------------


class TestStreamResponseHeaders:
    """SSE stream has correct response headers."""

    @pytest.mark.asyncio
    async def test_response_headers(self) -> None:
        """Stream response includes correct SSE headers."""
        app = _create_test_app()
        mock_workflow = _make_workflow()
        mock_task = _make_task(status=TaskStatus.COMPLETED)

        events = [
            _make_task_event(
                event_type="completed",
                data={"result": {}},
                sequence=1,
            ),
        ]

        mock_session = AsyncMock()

        async def mock_get(model, id_):
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                return mock_task
            return None

        mock_session.get = AsyncMock(side_effect=mock_get)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = events
        mock_session.execute = AsyncMock(return_value=mock_result)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"


# ---------------------------------------------------------------------------
# Test: Polling for new events (integration-style)
# ---------------------------------------------------------------------------


class TestStreamPolling:
    """Stream polls for new events and closes on terminal detection."""

    @pytest.mark.asyncio
    async def test_polling_picks_up_new_events(self) -> None:
        """Stream picks up new events during polling phase."""
        app = _create_test_app()
        mock_workflow = _make_workflow()

        # Track get calls to simulate task status change
        get_call_count = 0

        async def mock_get(model, id_):
            nonlocal get_call_count
            name = model.__name__ if hasattr(model, '__name__') else str(model)
            if name == "Workflow":
                return mock_workflow
            if name == "Task":
                get_call_count += 1
                # After polling starts, return completed
                if get_call_count > 2:
                    return _make_task(status=TaskStatus.COMPLETED)
                return _make_task(status=TaskStatus.RUNNING)
            return None

        mock_session = AsyncMock()
        mock_session.get = AsyncMock(side_effect=mock_get)
        mock_session.expire_all = MagicMock()

        # First query: no events (initial replay), second query: has completed event
        execute_call_count = 0
        completed_event = _make_task_event(
            event_type="completed",
            data={"result": {"output": "done"}},
            sequence=1,
        )

        async def mock_execute(stmt):
            nonlocal execute_call_count
            execute_call_count += 1
            mock_result = MagicMock()
            if execute_call_count <= 1:
                # Initial replay: no events
                mock_result.scalars.return_value.all.return_value = []
            else:
                # Polling: return completed event
                mock_result.scalars.return_value.all.return_value = [completed_event]
            return mock_result

        mock_session.execute = AsyncMock(side_effect=mock_execute)

        from fleet_api.database.connection import get_session

        async def mock_get_session():
            yield mock_session

        app.dependency_overrides[get_session] = mock_get_session

        with patch("fleet_api.tasks.sse._POLL_INTERVAL", 0.1):
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test", timeout=10.0
            ) as client:
                response = await client.get(
                    f"/workflows/{WORKFLOW_ID}/tasks/{TASK_ID}/stream",
                )

        assert response.status_code == 200
        parsed = _parse_sse_events(response.text)
        # Should contain the completed event picked up during polling
        completed_events = [e for e in parsed if e.get("event") == "completed"]
        assert len(completed_events) >= 1
