"""Tests for fleet_agent.streamer -- event streaming with mocked httpx."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, call, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.models import TaskEvent
from fleet_agent.streamer import EventStreamer


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def streamer(private_key: Ed25519PrivateKey) -> EventStreamer:
    return EventStreamer(
        fleet_api_url="https://fleet.example.com",
        agent_id="test-agent",
        private_key=private_key,
    )


async def _events_from_list(events: list[TaskEvent]) -> AsyncIterator[TaskEvent]:
    """Convert a list of TaskEvents into an async iterator."""
    for event in events:
        yield event


class TestEventStreamer:
    """EventStreamer POSTs events to fleet-api."""

    async def test_posts_events_to_correct_url_with_auth(
        self, streamer: EventStreamer
    ) -> None:
        """Events are POSTed to /tasks/{task_id}/events with signed headers."""
        events = [
            TaskEvent(event_type="progress", data={"pct": 50}, sequence=1),
        ]
        mock_response = httpx.Response(202)

        with patch("fleet_agent.streamer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await streamer.stream("task-42", _events_from_list(events))

            # Two posts: initial "running" + one user event.
            assert mock_client.post.call_count == 2

            # Check URL of first call (running event).
            first_call = mock_client.post.call_args_list[0]
            assert "task-42" in first_call[0][0]
            assert "/tasks/task-42/events" in first_call[0][0]

            # Check auth headers are present.
            headers = first_call[1]["headers"]
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Signature test-agent:")

    async def test_auto_increments_sequence_numbers(
        self, streamer: EventStreamer
    ) -> None:
        """Sequence numbers are re-assigned monotonically starting at 1."""
        events = [
            TaskEvent(event_type="progress", data={"pct": 25}, sequence=99),
            TaskEvent(event_type="progress", data={"pct": 75}, sequence=99),
            TaskEvent(event_type="completed", data={"result": "ok"}, sequence=99),
        ]
        mock_response = httpx.Response(202)

        posted_bodies: list[dict] = []

        with patch("fleet_agent.streamer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()

            async def capture_post(url, content=None, headers=None):
                if content:
                    posted_bodies.append(json.loads(content))
                return mock_response

            mock_client.post = AsyncMock(side_effect=capture_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await streamer.stream("task-42", _events_from_list(events))

        # 1 running + 3 user events = 4 total.
        assert len(posted_bodies) == 4
        sequences = [b["sequence"] for b in posted_bodies]
        assert sequences == [1, 2, 3, 4]

    async def test_sends_running_event_at_start(
        self, streamer: EventStreamer
    ) -> None:
        """An initial 'status: running' event is sent before user events."""
        posted_bodies: list[dict] = []
        mock_response = httpx.Response(202)

        with patch("fleet_agent.streamer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()

            async def capture_post(url, content=None, headers=None):
                if content:
                    posted_bodies.append(json.loads(content))
                return mock_response

            mock_client.post = AsyncMock(side_effect=capture_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await streamer.stream("task-42", _events_from_list([]))

        # Only the running event.
        assert len(posted_bodies) == 1
        assert posted_bodies[0]["event_type"] == "status"
        assert posted_bodies[0]["data"] == {"status": "running"}
        assert posted_bodies[0]["sequence"] == 1

    async def test_retries_on_transient_5xx(
        self, streamer: EventStreamer
    ) -> None:
        """5xx responses are retried with backoff."""
        call_count = 0

        async def flaky_post(url, content=None, headers=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return httpx.Response(503)
            return httpx.Response(202)

        with patch("fleet_agent.streamer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=flaky_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("fleet_agent.streamer.asyncio.sleep", new_callable=AsyncMock):
                await streamer.stream("task-42", _events_from_list([]))

        # Running event: 2 failures + 1 success = 3 calls.
        assert call_count == 3

    async def test_retries_on_connection_error(
        self, streamer: EventStreamer
    ) -> None:
        """Connection errors are retried."""
        call_count = 0

        async def flaky_post(url, content=None, headers=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            return httpx.Response(202)

        with patch("fleet_agent.streamer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=flaky_post)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("fleet_agent.streamer.asyncio.sleep", new_callable=AsyncMock):
                await streamer.stream("task-42", _events_from_list([]))

        # 1 failure + 1 success for the running event.
        assert call_count == 2
