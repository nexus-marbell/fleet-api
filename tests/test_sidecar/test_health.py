"""Tests for fleet_agent.health -- local health endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient

from fleet_agent.health import configure, get_app
from fleet_agent.poller import TaskPoller


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def poller(private_key: Ed25519PrivateKey) -> TaskPoller:
    return TaskPoller(
        fleet_api_url="https://fleet.example.com",
        agent_id="test-agent",
        private_key=private_key,
        interval=5,
    )


@pytest.fixture
async def health_client(poller: TaskPoller):
    """Async HTTP client for the health endpoint."""
    configure(
        poller=poller,
        fleet_api_url="https://fleet.example.com",
        agent_id="test-agent",
    )
    app = get_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


class TestHealthEndpoint:
    """GET /fleet/health reports sidecar status."""

    async def test_returns_healthy_when_poller_running_and_api_reachable(
        self, health_client: AsyncClient, poller: TaskPoller
    ) -> None:
        """Status is 'healthy' when poller is running and fleet-api responds."""
        poller._running = True

        with patch("fleet_agent.health._check_fleet_api", return_value=True):
            response = await health_client.get("/fleet/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["agent_id"] == "test-agent"
        assert data["fleet_api_url"] == "https://fleet.example.com"
        assert data["fleet_api_reachable"] is True
        assert data["poller_running"] is True
        assert isinstance(data["active_tasks"], int)
        assert isinstance(data["uptime_seconds"], int)

    async def test_returns_unhealthy_when_fleet_api_unreachable(
        self, health_client: AsyncClient, poller: TaskPoller
    ) -> None:
        """Status is 'unhealthy' when fleet-api cannot be reached."""
        poller._running = True

        with patch("fleet_agent.health._check_fleet_api", return_value=False):
            response = await health_client.get("/fleet/health")

        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["fleet_api_reachable"] is False

    async def test_returns_unhealthy_when_poller_not_running(
        self, health_client: AsyncClient, poller: TaskPoller
    ) -> None:
        """Status is 'unhealthy' when the poller has not started."""
        poller._running = False

        with patch("fleet_agent.health._check_fleet_api", return_value=True):
            response = await health_client.get("/fleet/health")

        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["poller_running"] is False

    async def test_reports_active_task_count(
        self, health_client: AsyncClient, poller: TaskPoller
    ) -> None:
        """Active tasks reflects in-flight count from poller."""
        poller._running = True
        poller._in_flight = {"t-1", "t-2", "t-3"}

        with patch("fleet_agent.health._check_fleet_api", return_value=True):
            response = await health_client.get("/fleet/health")

        data = response.json()
        assert data["active_tasks"] == 3
