"""Tests for callback delivery (fleet_api.tasks.callbacks).

Covers:
  - sign_callback produces valid Ed25519 signature
  - Signature verification round-trip
  - deliver_callback sends correct headers
  - deliver_callback retries on failure (mock httpx)
  - deliver_callback does nothing if callback_url is None
  - callback_url stored on task creation (via route)
  - callback triggered on task completion event
  - callback triggered on task failure event
  - Manifest includes server_public_key (not None)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient, Response

from fleet_api.app import create_app
from fleet_api.crypto import get_server_public_key, reset_keypair, sign_callback
from fleet_api.middleware.auth import AuthenticatedAgent, get_agent_lookup, require_auth
from fleet_api.tasks.callbacks import (
    build_callback_payload,
    deliver_callback,
    schedule_callback,
)
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus
from fleet_api.tasks.routes import get_task_service

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_keypair():
    """Reset the server keypair for test isolation."""
    reset_keypair()
    yield
    reset_keypair()


def _make_task(
    task_id: str = "task-cb001",
    workflow_id: str = "wf-test",
    status: TaskStatus = TaskStatus.COMPLETED,
    callback_url: str | None = "https://agent.example.com/callback",
    result: dict[str, Any] | None = None,
    completed_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Task with callback fields."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = workflow_id
    task.status = status
    task.callback_url = callback_url
    task.result = result or {"output": "done"}
    task.completed_at = completed_at or datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)
    task.principal_agent_id = "caller-agent"
    task.executor_agent_id = "executor-agent"
    task.input = {"rule": 30}
    task.priority = TaskPriority.NORMAL
    task.timeout_seconds = 300
    task.idempotency_key = None
    task.created_at = datetime(2026, 3, 7, 14, 30, 0, tzinfo=UTC)
    task.metadata_ = None
    task.started_at = datetime(2026, 3, 7, 14, 31, 0, tzinfo=UTC)
    return task


# ---------------------------------------------------------------------------
# Signature tests
# ---------------------------------------------------------------------------


class TestSignCallback:
    def test_sign_produces_valid_signature(self) -> None:
        """sign_callback output can be verified by the server's public key."""
        method = "POST"
        path = "/agent/callback"
        timestamp = "2026-03-07T15:00:00+00:00"
        body = b'{"task_id": "task-cb001", "status": "completed"}'

        sig_b64 = sign_callback(method, path, timestamp, body)

        # Verify
        body_hash = hashlib.sha256(body).hexdigest()
        signing_string = f"{method}\n{path}\n{timestamp}\n{body_hash}".encode()
        pub = get_server_public_key()
        sig_bytes = base64.b64decode(sig_b64)
        pub.verify(sig_bytes, signing_string)  # no exception = pass

    def test_sign_verify_round_trip(self) -> None:
        """Full round-trip: sign, decode, verify matches signing string."""
        body = b'{"status": "failed"}'
        sig_b64 = sign_callback("POST", "/cb", "2026-03-07T12:00:00Z", body)

        body_hash = hashlib.sha256(body).hexdigest()
        expected = f"POST\n/cb\n2026-03-07T12:00:00Z\n{body_hash}".encode()

        pub = get_server_public_key()
        pub.verify(base64.b64decode(sig_b64), expected)


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


class TestBuildCallbackPayload:
    def test_payload_fields(self) -> None:
        """Payload contains task_id, workflow_id, status, result, completed_at."""
        task = _make_task()
        payload = build_callback_payload(task)

        assert payload["task_id"] == "task-cb001"
        assert payload["workflow_id"] == "wf-test"
        assert payload["status"] == "completed"
        assert payload["result"] == {"output": "done"}
        assert payload["completed_at"] is not None

    def test_payload_with_none_result(self) -> None:
        """Payload handles None result gracefully."""
        task = _make_task(result=None)
        task.result = None
        payload = build_callback_payload(task)
        assert payload["result"] is None


# ---------------------------------------------------------------------------
# deliver_callback
# ---------------------------------------------------------------------------


class TestDeliverCallback:
    @pytest.mark.asyncio
    async def test_no_op_when_no_callback_url(self) -> None:
        """Returns True immediately when callback_url is None."""
        task = _make_task(callback_url=None)
        result = await deliver_callback(task)
        assert result is True

    @pytest.mark.asyncio
    async def test_successful_delivery(self) -> None:
        """Sends POST with correct headers on successful delivery."""
        task = _make_task()

        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("fleet_api.tasks.callbacks.httpx.AsyncClient", return_value=mock_client):
            result = await deliver_callback(task)

        assert result is True
        mock_client.post.assert_called_once()

        # Check headers
        call_kwargs = mock_client.post.call_args
        headers = call_kwargs.kwargs["headers"]
        assert headers["Content-Type"] == "application/json"
        assert "X-Fleet-Signature" in headers
        assert "X-Fleet-Timestamp" in headers
        assert headers["X-Fleet-Key-Id"] == "fleet-api"

    @pytest.mark.asyncio
    async def test_sends_correct_body(self) -> None:
        """Callback body is a JSON-serialized task payload."""
        task = _make_task()

        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("fleet_api.tasks.callbacks.httpx.AsyncClient", return_value=mock_client):
            await deliver_callback(task)

        call_kwargs = mock_client.post.call_args
        body = call_kwargs.kwargs["content"]
        payload = json.loads(body)
        assert payload["task_id"] == "task-cb001"
        assert payload["status"] == "completed"

    @pytest.mark.asyncio
    async def test_signature_verifies(self) -> None:
        """The X-Fleet-Signature header can be verified with the server public key."""
        task = _make_task()

        captured_kwargs: dict = {}
        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 200

        mock_client = AsyncMock()

        async def capture_post(*args: Any, **kwargs: Any) -> MagicMock:
            captured_kwargs.update(kwargs)
            return mock_response

        mock_client.post = capture_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("fleet_api.tasks.callbacks.httpx.AsyncClient", return_value=mock_client):
            await deliver_callback(task)

        headers = captured_kwargs["headers"]
        body = captured_kwargs["content"]
        sig_b64 = headers["X-Fleet-Signature"]
        timestamp = headers["X-Fleet-Timestamp"]

        # Rebuild signing string
        body_hash = hashlib.sha256(body).hexdigest()
        signing_string = f"POST\n/callback\n{timestamp}\n{body_hash}".encode()

        pub = get_server_public_key()
        pub.verify(base64.b64decode(sig_b64), signing_string)

    @pytest.mark.asyncio
    async def test_retries_on_failure(self) -> None:
        """Retries up to MAX_ATTEMPTS on non-2xx responses."""
        task = _make_task()

        mock_response_500 = MagicMock(spec=Response)
        mock_response_500.status_code = 500

        mock_response_200 = MagicMock(spec=Response)
        mock_response_200.status_code = 200

        call_count = 0

        mock_client = AsyncMock()

        async def post_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return mock_response_500
            return mock_response_200

        mock_client.post = post_side_effect
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fleet_api.tasks.callbacks.httpx.AsyncClient", return_value=mock_client),
            patch("fleet_api.tasks.callbacks.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await deliver_callback(task)

        assert result is True
        assert call_count == 3  # 2 failures + 1 success

    @pytest.mark.asyncio
    async def test_returns_false_after_all_retries_exhausted(self) -> None:
        """Returns False when all retry attempts fail."""
        task = _make_task()

        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fleet_api.tasks.callbacks.httpx.AsyncClient", return_value=mock_client),
            patch("fleet_api.tasks.callbacks.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await deliver_callback(task)

        assert result is False
        assert mock_client.post.call_count == 4  # 1 initial + 3 retries

    @pytest.mark.asyncio
    async def test_retries_on_connection_error(self) -> None:
        """Retries on network/connection errors."""
        task = _make_task()

        mock_response = MagicMock(spec=Response)
        mock_response.status_code = 200

        call_count = 0

        mock_client = AsyncMock()

        async def post_side_effect(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("refused")
            return mock_response

        mock_client.post = post_side_effect
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("fleet_api.tasks.callbacks.httpx.AsyncClient", return_value=mock_client),
            patch("fleet_api.tasks.callbacks.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await deliver_callback(task)

        assert result is True
        assert call_count == 2


# ---------------------------------------------------------------------------
# schedule_callback
# ---------------------------------------------------------------------------


class TestScheduleCallback:
    def test_returns_none_when_no_callback_url(self) -> None:
        """schedule_callback returns None if task has no callback_url."""
        task = _make_task(callback_url=None)
        result = schedule_callback(task)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_asyncio_task(self) -> None:
        """schedule_callback returns an asyncio.Task when callback_url is set."""
        task = _make_task()

        # Patch deliver_callback to be a no-op
        with patch(
            "fleet_api.tasks.callbacks.deliver_callback",
            new_callable=AsyncMock,
            return_value=True,
        ):
            bg = schedule_callback(task)
            assert isinstance(bg, asyncio.Task)
            assert bg.get_name() == "callback-task-cb001"
            # Let it finish
            await bg


# ---------------------------------------------------------------------------
# callback_url through task creation route
# ---------------------------------------------------------------------------


class MockAgentLookup:
    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


class TestCallbackUrlWiring:
    @pytest.mark.asyncio
    async def test_callback_url_passed_to_service(self) -> None:
        """POST /workflows/{id}/run passes callback_url to service.create_task."""
        mock_service = MagicMock()
        mock_task = _make_task()
        mock_wf = MagicMock()
        mock_wf.id = "wf-test"
        mock_wf.owner_agent_id = "owner"
        mock_wf.estimated_duration_seconds = 15
        mock_service.create_task = AsyncMock(return_value=(mock_task, mock_wf, False))
        mock_service.build_task_response = MagicMock(
            return_value={
                "task_id": "task-cb001",
                "status": "accepted",
                "_links": {"self": {"href": "/test"}},
            }
        )

        app = create_app()

        async def mock_auth() -> AuthenticatedAgent:
            mock_key = MagicMock(spec=Ed25519PublicKey)
            return AuthenticatedAgent(agent_id="test-agent", public_key=mock_key)

        app.dependency_overrides[require_auth] = mock_auth
        app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
        app.dependency_overrides[get_task_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/workflows/wf-test/run",
                json={
                    "input": {"rule": 30},
                    "callback_url": "https://agent.example.com/results",
                },
            )

        assert response.status_code == 202
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["callback_url"] == "https://agent.example.com/results"

    @pytest.mark.asyncio
    async def test_callback_url_defaults_to_none(self) -> None:
        """POST /workflows/{id}/run without callback_url passes None."""
        mock_service = MagicMock()
        mock_task = _make_task(callback_url=None)
        mock_wf = MagicMock()
        mock_wf.id = "wf-test"
        mock_wf.owner_agent_id = "owner"
        mock_wf.estimated_duration_seconds = 15
        mock_service.create_task = AsyncMock(return_value=(mock_task, mock_wf, False))
        mock_service.build_task_response = MagicMock(
            return_value={
                "task_id": "task-cb001",
                "status": "accepted",
                "_links": {"self": {"href": "/test"}},
            }
        )

        app = create_app()

        async def mock_auth() -> AuthenticatedAgent:
            mock_key = MagicMock(spec=Ed25519PublicKey)
            return AuthenticatedAgent(agent_id="test-agent", public_key=mock_key)

        app.dependency_overrides[require_auth] = mock_auth
        app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
        app.dependency_overrides[get_task_service] = lambda: mock_service

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/workflows/wf-test/run",
                json={"input": {"rule": 30}},
            )

        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["callback_url"] is None


# ---------------------------------------------------------------------------
# Integration: process_sidecar_event → schedule_callback wiring
# ---------------------------------------------------------------------------


class TestProcessSidecarEventCallbackWiring:
    """Verifies the glue in service.py that calls schedule_callback on terminal events."""

    @pytest.mark.asyncio
    async def test_completed_event_triggers_schedule_callback(self) -> None:
        """process_sidecar_event with 'completed' calls schedule_callback when callback_url set."""
        from fleet_api.tasks.service import process_sidecar_event

        task = MagicMock(spec=Task)
        task.id = "task-int001"
        task.status = TaskStatus.RUNNING
        task.executor_agent_id = "exec-agent"
        task.callback_url = "https://agent.example.com/callback"
        task.started_at = datetime(2026, 3, 7, 14, 0, 0, tzinfo=UTC)
        task.completed_at = None
        task.result = None
        task.metadata_ = None

        # Mock transition_to to actually change the status
        def do_transition(new_status: TaskStatus) -> None:
            task.status = new_status
            task.completed_at = datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)

        task.transition_to = MagicMock(side_effect=do_transition)

        # Mock session
        session = AsyncMock()
        session.get = AsyncMock(return_value=task)

        # Mock max sequence query
        mock_scalar = MagicMock()
        mock_scalar.scalar_one = MagicMock(return_value=0)
        session.execute = AsyncMock(return_value=mock_scalar)

        with patch("fleet_api.tasks.sidecar.schedule_callback") as mock_schedule:
            event, returned_task = await process_sidecar_event(
                session=session,
                task_id="task-int001",
                event_type="completed",
                data={"result": {"output": "done"}},
                sequence=1,
                executor_agent_id="exec-agent",
            )

        mock_schedule.assert_called_once_with(task)

    @pytest.mark.asyncio
    async def test_no_callback_when_url_is_none(self) -> None:
        """process_sidecar_event with terminal event skips callback when callback_url is None."""
        from fleet_api.tasks.service import process_sidecar_event

        task = MagicMock(spec=Task)
        task.id = "task-int002"
        task.status = TaskStatus.RUNNING
        task.executor_agent_id = "exec-agent"
        task.callback_url = None
        task.started_at = datetime(2026, 3, 7, 14, 0, 0, tzinfo=UTC)
        task.completed_at = None
        task.result = None
        task.metadata_ = None

        def do_transition(new_status: TaskStatus) -> None:
            task.status = new_status
            task.completed_at = datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)

        task.transition_to = MagicMock(side_effect=do_transition)

        session = AsyncMock()
        session.get = AsyncMock(return_value=task)
        mock_scalar = MagicMock()
        mock_scalar.scalar_one = MagicMock(return_value=0)
        session.execute = AsyncMock(return_value=mock_scalar)

        with patch("fleet_api.tasks.sidecar.schedule_callback") as mock_schedule:
            await process_sidecar_event(
                session=session,
                task_id="task-int002",
                event_type="completed",
                data={"result": {"output": "done"}},
                sequence=1,
                executor_agent_id="exec-agent",
            )

        mock_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_event_triggers_schedule_callback(self) -> None:
        """process_sidecar_event with 'failed' also triggers schedule_callback."""
        from fleet_api.tasks.service import process_sidecar_event

        task = MagicMock(spec=Task)
        task.id = "task-int003"
        task.status = TaskStatus.RUNNING
        task.executor_agent_id = "exec-agent"
        task.callback_url = "https://agent.example.com/callback"
        task.started_at = datetime(2026, 3, 7, 14, 0, 0, tzinfo=UTC)
        task.completed_at = None
        task.result = None
        task.metadata_ = None

        def do_transition(new_status: TaskStatus) -> None:
            task.status = new_status
            task.completed_at = datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)

        task.transition_to = MagicMock(side_effect=do_transition)

        session = AsyncMock()
        session.get = AsyncMock(return_value=task)
        mock_scalar = MagicMock()
        mock_scalar.scalar_one = MagicMock(return_value=0)
        session.execute = AsyncMock(return_value=mock_scalar)

        with patch("fleet_api.tasks.sidecar.schedule_callback") as mock_schedule:
            await process_sidecar_event(
                session=session,
                task_id="task-int003",
                event_type="failed",
                data={"error_code": "TIMEOUT", "message": "Task timed out"},
                sequence=1,
                executor_agent_id="exec-agent",
            )

        mock_schedule.assert_called_once_with(task)


# ---------------------------------------------------------------------------
# Manifest includes server_public_key
# ---------------------------------------------------------------------------


class TestManifestServerKey:
    @pytest.mark.asyncio
    async def test_manifest_includes_server_public_key(self) -> None:
        """GET /manifest returns non-None server_public_key in auth section."""
        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/manifest")

        assert response.status_code == 200
        data = response.json()
        server_key = data["auth"]["server_public_key"]
        assert server_key is not None
        assert server_key.startswith("-----BEGIN PUBLIC KEY-----")
        assert server_key.strip().endswith("-----END PUBLIC KEY-----")
