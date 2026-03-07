"""Tests for POST /agents/{agent_id}/heartbeat endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from httpx import ASGITransport, AsyncClient

from fleet_api.agents.models import AgentStatus
from fleet_api.app import create_app
from fleet_api.database.connection import get_session
from fleet_api.middleware.auth import get_agent_lookup
from tests.auth_helpers import sign_request
from tests.test_agents.conftest import generate_keypair, make_fake_agent

# ---------------------------------------------------------------------------
# Mock lookup that knows about our test agents
# ---------------------------------------------------------------------------


class HeartbeatMockLookup:
    """Agent lookup that resolves registered test agents."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._suspended: set[str] = set()

    def register(self, agent_id: str, public_key: Ed25519PublicKey) -> None:
        self._keys[agent_id] = public_key

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return self._keys.get(agent_id)

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return agent_id in self._suspended


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def mock_lookup() -> HeartbeatMockLookup:
    return HeartbeatMockLookup()


@pytest.fixture
def keypair_and_id(mock_lookup: HeartbeatMockLookup) -> tuple[Ed25519PrivateKey, str, str]:
    """Register a test agent, return (private_key, agent_id, pub_b64)."""
    private_key, pub_b64 = generate_keypair()
    agent_id = "heartbeat-agent"
    mock_lookup.register(agent_id, private_key.public_key())
    return private_key, agent_id, pub_b64


@pytest.fixture
def app_with_mocks(mock_session: AsyncMock, mock_lookup: HeartbeatMockLookup) -> Any:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_agent_lookup] = lambda: mock_lookup
    return app


@pytest.fixture
async def client(app_with_mocks: Any) -> AsyncClient:
    transport = ASGITransport(app=app_with_mocks)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Happy path — heartbeat (200)
# ---------------------------------------------------------------------------


class TestHeartbeatHappyPath:
    @pytest.mark.asyncio
    async def test_heartbeat_returns_200(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Valid heartbeat returns 200 with agent status."""
        private_key, agent_id, pub_b64 = keypair_and_id
        now = datetime.now(UTC)

        agent = make_fake_agent(
            agent_id=agent_id, public_key_b64=pub_b64, status=AgentStatus.ACTIVE
        )
        agent.last_heartbeat = now

        # First call: get_agent in heartbeat; configure return for both calls
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = f"/agents/{agent_id}/heartbeat"
        headers = sign_request("POST", path, None, private_key, agent_id)
        response = await client.post(path, headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == agent_id
        assert data["status"] == "active"
        assert data["last_heartbeat"] is not None

    @pytest.mark.asyncio
    async def test_first_heartbeat_activates(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """First heartbeat transitions status from registered to active."""
        private_key, agent_id, pub_b64 = keypair_and_id

        agent = make_fake_agent(
            agent_id=agent_id, public_key_b64=pub_b64, status=AgentStatus.REGISTERED
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        mock_session.execute = AsyncMock(return_value=mock_result)

        # After heartbeat, simulate the service changing status
        async def fake_refresh(obj: Any) -> None:
            pass  # status already mutated by service

        mock_session.refresh = AsyncMock(side_effect=fake_refresh)

        path = f"/agents/{agent_id}/heartbeat"
        headers = sign_request("POST", path, None, private_key, agent_id)
        response = await client.post(path, headers=headers)

        assert response.status_code == 200
        # The service mutates the agent directly
        assert agent.status == AgentStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_heartbeat_includes_links(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Heartbeat response includes HATEOAS links."""
        private_key, agent_id, pub_b64 = keypair_and_id
        now = datetime.now(UTC)

        agent = make_fake_agent(
            agent_id=agent_id, public_key_b64=pub_b64, status=AgentStatus.ACTIVE
        )
        agent.last_heartbeat = now

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = f"/agents/{agent_id}/heartbeat"
        headers = sign_request("POST", path, None, private_key, agent_id)
        response = await client.post(path, headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "_links" in data
        assert "self" in data["_links"]


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestHeartbeatAuth:
    @pytest.mark.asyncio
    async def test_heartbeat_requires_auth(
        self, client: AsyncClient
    ) -> None:
        """Heartbeat without auth returns 401."""
        response = await client.post("/agents/some-agent/heartbeat")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_heartbeat_agent_mismatch(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        mock_lookup: HeartbeatMockLookup,
    ) -> None:
        """Agent A cannot heartbeat for Agent B — returns 403."""
        # Register agent-a
        private_key_a, _ = generate_keypair()
        mock_lookup.register("agent-a", private_key_a.public_key())

        # Sign as agent-a but heartbeat for agent-b
        path = "/agents/agent-b/heartbeat"
        headers = sign_request("POST", path, None, private_key_a, "agent-a")
        response = await client.post(path, headers=headers)

        assert response.status_code == 403
        data = response.json()
        assert data["code"] == "NOT_AUTHORIZED"


# ---------------------------------------------------------------------------
# Agent not found
# ---------------------------------------------------------------------------


class TestHeartbeatNotFound:
    @pytest.mark.asyncio
    async def test_heartbeat_unknown_agent(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Heartbeat for an agent not in the DB returns 404."""
        private_key, agent_id, _ = keypair_and_id

        # Agent exists in lookup (for auth) but not in DB
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = f"/agents/{agent_id}/heartbeat"
        headers = sign_request("POST", path, None, private_key, agent_id)
        response = await client.post(path, headers=headers)

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "ENDPOINT_NOT_FOUND"
