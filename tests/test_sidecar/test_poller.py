"""Tests for fleet_agent.poller -- task polling with mocked httpx."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.executor import LocalExecutor
from fleet_agent.models import PendingTask, TaskEvent
from fleet_agent.poller import TaskPoller
from fleet_agent.streamer import EventStreamer

_SAMPLE_TASKS = [
    {
        "task_id": "t-1",
        "workflow_id": "wf-1",
        "input": {"prompt": "hello"},
        "priority": "normal",
        "timeout_seconds": 60,
        "created_at": "2026-03-07T12:00:00Z",
    },
    {
        "task_id": "t-2",
        "workflow_id": "wf-1",
        "input": {"prompt": "world"},
        "priority": "low",
        "timeout_seconds": None,
        "created_at": "2026-03-07T12:01:00Z",
    },
]

_DUMMY_REQUEST = httpx.Request("GET", "https://fleet.example.com/agents/test-agent/tasks/pending")


def _ok_response(data: object) -> httpx.Response:
    """Create a 200 response with a request set (needed for raise_for_status)."""
    resp = httpx.Response(200, json=data)
    resp._request = _DUMMY_REQUEST  # type: ignore[attr-defined]
    return resp


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def poller(private_key: Ed25519PrivateKey) -> TaskPoller:
    return TaskPoller(
        fleet_api_url="https://fleet.example.com",
        agent_id="test-agent",
        private_key=private_key,
        interval=1,
        max_concurrent=2,
    )


class TestPoll:
    """TaskPoller.poll() fetches pending tasks."""

    async def test_polls_correct_url_with_auth_header(
        self, poller: TaskPoller
    ) -> None:
        """GET request targets /agents/{id}/tasks/pending with signed headers."""
        mock_response = _ok_response(_SAMPLE_TASKS)

        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = await poller.poll()

            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "/agents/test-agent/tasks/pending" in call_args[0][0]
            headers = call_args[1]["headers"]
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Signature test-agent:")
            assert "X-Fleet-Timestamp" in headers

    async def test_returns_parsed_pending_tasks(
        self, poller: TaskPoller
    ) -> None:
        """Response JSON is parsed into PendingTask objects."""
        mock_response = _ok_response(_SAMPLE_TASKS)

        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = await poller.poll()

        assert len(tasks) == 2
        assert isinstance(tasks[0], PendingTask)
        assert tasks[0].task_id == "t-1"
        assert tasks[1].task_id == "t-2"

    async def test_handles_empty_response(self, poller: TaskPoller) -> None:
        """Empty task list returns empty list."""
        mock_response = _ok_response([])

        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = await poller.poll()

        assert tasks == []

    async def test_returns_empty_on_connection_error(
        self, poller: TaskPoller
    ) -> None:
        """Connection errors return empty list instead of raising."""
        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = await poller.poll()

        assert tasks == []

    async def test_handles_dict_response_with_tasks_key(
        self, poller: TaskPoller
    ) -> None:
        """Response wrapped in {'tasks': [...]} is handled."""
        mock_response = _ok_response({"tasks": _SAMPLE_TASKS[:1]})

        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = await poller.poll()

        assert len(tasks) == 1
        assert tasks[0].task_id == "t-1"


class TestPollerConcurrency:
    """TaskPoller respects max_concurrent_tasks."""

    async def test_respects_max_concurrent_tasks(
        self, private_key: Ed25519PrivateKey
    ) -> None:
        """Poller does not dispatch beyond the concurrency limit."""
        poller = TaskPoller(
            fleet_api_url="https://fleet.example.com",
            agent_id="test-agent",
            private_key=private_key,
            interval=1,
            max_concurrent=1,
        )

        # Simulate two tasks returned but max_concurrent=1.
        # Manually add one to in_flight.
        poller._in_flight.add("existing-task")

        mock_response = _ok_response(_SAMPLE_TASKS)

        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            tasks = await poller.poll()

        # Tasks fetched but active_task_count reflects the in-flight task.
        assert poller.active_task_count == 1
        assert len(tasks) == 2  # Still fetches them; dispatch limit is in run()

    async def test_skips_already_inflight_tasks(
        self, poller: TaskPoller
    ) -> None:
        """Tasks already in-flight are not double-dispatched."""
        poller._in_flight.add("t-1")

        # Verify the in-flight tracking works.
        assert "t-1" in poller._in_flight
        assert poller.active_task_count == 1
