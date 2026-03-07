"""Tests for GET /agents/{agent_id} endpoint."""

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
# Mock lookup
# ---------------------------------------------------------------------------


class GetAgentMockLookup:
    """Agent lookup for get-agent tests."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}

    def register(self, agent_id: str, public_key: Ed25519PublicKey) -> None:
        self._keys[agent_id] = public_key

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return self._keys.get(agent_id)

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


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
def mock_lookup() -> GetAgentMockLookup:
    return GetAgentMockLookup()


@pytest.fixture
def keypair_and_id(mock_lookup: GetAgentMockLookup) -> tuple[Ed25519PrivateKey, str, str]:
    """Register a test agent for auth, return (private_key, agent_id, pub_b64)."""
    private_key, pub_b64 = generate_keypair()
    agent_id = "viewer-agent"
    mock_lookup.register(agent_id, private_key.public_key())
    return private_key, agent_id, pub_b64


@pytest.fixture
def app_with_mocks(mock_session: AsyncMock, mock_lookup: GetAgentMockLookup) -> Any:
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
# Happy path — get agent (200)
# ---------------------------------------------------------------------------


class TestGetAgentHappyPath:
    @pytest.mark.asyncio
    async def test_get_agent_returns_200(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Authenticated request for existing agent returns 200."""
        private_key, viewer_id, pub_b64 = keypair_and_id

        target = make_fake_agent(
            agent_id="target-agent",
            public_key_b64=pub_b64,
            display_name="Target Agent",
            capabilities=["execute", "monitor"],
            status=AgentStatus.ACTIVE,
            last_heartbeat=datetime(2026, 1, 2, tzinfo=UTC),
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = target
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = "/agents/target-agent"
        headers = sign_request("GET", path, None, private_key, viewer_id)
        response = await client.get(path, headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == "target-agent"
        assert data["display_name"] == "Target Agent"
        assert data["public_key"] == pub_b64
        assert data["capabilities"] == ["execute", "monitor"]
        assert data["status"] == "active"
        assert data["last_heartbeat"] is not None

    @pytest.mark.asyncio
    async def test_get_agent_cross_agent_view(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Any authenticated agent can view another agent's profile."""
        private_key, viewer_id, pub_b64 = keypair_and_id

        other_agent = make_fake_agent(
            agent_id="other-agent",
            public_key_b64="other-key-b64",
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = other_agent
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = "/agents/other-agent"
        headers = sign_request("GET", path, None, private_key, viewer_id)
        response = await client.get(path, headers=headers)

        # Authenticated agent can view any other agent
        assert response.status_code == 200
        assert response.json()["agent_id"] == "other-agent"

    @pytest.mark.asyncio
    async def test_get_agent_includes_links(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Agent response includes HATEOAS _links."""
        private_key, viewer_id, pub_b64 = keypair_and_id

        agent = make_fake_agent(agent_id="linked-agent", public_key_b64=pub_b64)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = "/agents/linked-agent"
        headers = sign_request("GET", path, None, private_key, viewer_id)
        response = await client.get(path, headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "_links" in data
        assert "self" in data["_links"]
        assert "heartbeat" in data["_links"]
        assert data["_links"]["self"]["href"] == "/agents/linked-agent"


# ---------------------------------------------------------------------------
# Auth enforcement
# ---------------------------------------------------------------------------


class TestGetAgentAuth:
    @pytest.mark.asyncio
    async def test_get_agent_requires_auth(self, client: AsyncClient) -> None:
        """GET /agents/{agent_id} without auth returns 401."""
        response = await client.get("/agents/any-agent")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Not found (404)
# ---------------------------------------------------------------------------


class TestGetAgentNotFound:
    @pytest.mark.asyncio
    async def test_unknown_agent_returns_404(
        self,
        client: AsyncClient,
        mock_session: AsyncMock,
        keypair_and_id: tuple[Ed25519PrivateKey, str, str],
    ) -> None:
        """Requesting a non-existent agent returns 404 ENDPOINT_NOT_FOUND."""
        private_key, viewer_id, _ = keypair_and_id

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        path = "/agents/ghost-agent"
        headers = sign_request("GET", path, None, private_key, viewer_id)
        response = await client.get(path, headers=headers)

        assert response.status_code == 404
        data = response.json()
        assert data["code"] == "ENDPOINT_NOT_FOUND"
        assert "ghost-agent" in data["message"]
