"""Tests for POST /agents/register endpoint."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import ASGITransport, AsyncClient

from fleet_api.agents.models import AgentStatus
from fleet_api.app import create_app
from fleet_api.database.connection import get_session
from fleet_api.middleware.auth import get_agent_lookup
from tests.test_agents.conftest import generate_keypair, make_fake_agent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """Passthrough lookup that allows unprotected paths through."""

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return None

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return False


@pytest.fixture
def mock_session() -> AsyncMock:
    """Mock async SQLAlchemy session."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def app_with_mocks(mock_session: AsyncMock) -> Any:
    """Create app with mocked DB session and agent lookup."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_agent_lookup] = lambda: MockAgentLookup()
    return app


@pytest.fixture
async def client(app_with_mocks: Any) -> AsyncClient:
    transport = ASGITransport(app=app_with_mocks, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Happy path — new registration (201)
# ---------------------------------------------------------------------------


class TestRegisterNewAgent:
    @pytest.mark.asyncio
    async def test_register_returns_201(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """New agent registration returns 201 with agent details."""
        _, pub_b64 = generate_keypair()

        # Mock: no existing agent
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        # Mock refresh to set fields on the ORM object
        async def fake_refresh(obj: Any) -> None:
            obj.status = AgentStatus.REGISTERED
            obj.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
            obj.last_heartbeat = None

        mock_session.refresh = AsyncMock(side_effect=fake_refresh)

        body = {
            "agent_id": "my-agent-01",
            "display_name": "My Agent",
            "public_key": pub_b64,
            "capabilities": ["execute", "monitor"],
        }
        response = await client.post("/agents/register", json=body)

        assert response.status_code == 201
        data = response.json()
        assert data["agent_id"] == "my-agent-01"
        assert data["display_name"] == "My Agent"
        assert data["public_key"] == pub_b64
        assert data["capabilities"] == ["execute", "monitor"]
        assert data["status"] == "registered"

    @pytest.mark.asyncio
    async def test_register_includes_onboarding(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Registration response includes Pattern 13 onboarding steps."""
        _, pub_b64 = generate_keypair()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        async def fake_refresh(obj: Any) -> None:
            obj.status = AgentStatus.REGISTERED
            obj.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
            obj.last_heartbeat = None

        mock_session.refresh = AsyncMock(side_effect=fake_refresh)

        body = {"agent_id": "onboard-agent", "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)

        assert response.status_code == 201
        data = response.json()
        assert "onboarding" in data
        assert len(data["onboarding"]) == 3
        assert data["onboarding"][0]["step"] == 1
        assert "heartbeat" in data["onboarding"][0]["action"].lower()

    @pytest.mark.asyncio
    async def test_register_includes_hateoas_links(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Registration response includes HATEOAS _links."""
        _, pub_b64 = generate_keypair()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        async def fake_refresh(obj: Any) -> None:
            obj.status = AgentStatus.REGISTERED
            obj.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
            obj.last_heartbeat = None

        mock_session.refresh = AsyncMock(side_effect=fake_refresh)

        body = {"agent_id": "links-agent", "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)

        assert response.status_code == 201
        data = response.json()
        assert "_links" in data
        assert "self" in data["_links"]
        assert "heartbeat" in data["_links"]
        assert data["_links"]["self"]["href"] == "/agents/links-agent"


# ---------------------------------------------------------------------------
# Idempotent re-registration (200)
# ---------------------------------------------------------------------------


class TestIdempotentReRegistration:
    @pytest.mark.asyncio
    async def test_same_key_returns_200(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Re-registering with the same public key returns 200."""
        _, pub_b64 = generate_keypair()
        existing = make_fake_agent(agent_id="idem-agent", public_key_b64=pub_b64)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute = AsyncMock(return_value=mock_result)

        body = {"agent_id": "idem-agent", "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)

        # Idempotent re-registration returns 200
        assert response.status_code == 200
        data = response.json()
        assert data["agent_id"] == "idem-agent"


# ---------------------------------------------------------------------------
# Conflict — different public key (409)
# ---------------------------------------------------------------------------


class TestRegistrationConflict:
    @pytest.mark.asyncio
    async def test_different_key_returns_409(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Different public key for same agent_id returns 409 AGENT_EXISTS."""
        _, original_b64 = generate_keypair()
        _, different_b64 = generate_keypair()

        existing = make_fake_agent(agent_id="conflict-agent", public_key_b64=original_b64)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        mock_session.execute = AsyncMock(return_value=mock_result)

        body = {"agent_id": "conflict-agent", "public_key": different_b64}
        response = await client.post("/agents/register", json=body)

        assert response.status_code == 409
        data = response.json()
        assert data["code"] == "AGENT_EXISTS"
        assert "different public key" in data["message"]


# ---------------------------------------------------------------------------
# Validation errors (422)
# ---------------------------------------------------------------------------


class TestRegistrationValidation:
    @pytest.mark.asyncio
    async def test_empty_agent_id(self, client: AsyncClient) -> None:
        """Empty agent_id returns 422."""
        _, pub_b64 = generate_keypair()
        body = {"agent_id": "", "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_agent_id_too_long(self, client: AsyncClient) -> None:
        """agent_id over 128 chars returns 422."""
        _, pub_b64 = generate_keypair()
        body = {"agent_id": "a" * 129, "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_agent_id_invalid_characters(self, client: AsyncClient) -> None:
        """agent_id with special characters returns 422."""
        _, pub_b64 = generate_keypair()
        body = {"agent_id": "agent@bad!", "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_agent_id_starts_with_hyphen(self, client: AsyncClient) -> None:
        """agent_id starting with hyphen returns 422."""
        _, pub_b64 = generate_keypair()
        body = {"agent_id": "-bad-start", "public_key": pub_b64}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_base64_public_key(self, client: AsyncClient) -> None:
        """Invalid base64 in public_key returns 422."""
        body = {"agent_id": "good-agent", "public_key": "not!valid!base64!!!"}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_wrong_size_public_key(self, client: AsyncClient) -> None:
        """Public key that decodes to != 32 bytes returns 422."""
        bad_key = base64.b64encode(b"only16byteslong!").decode()
        body = {"agent_id": "good-agent", "public_key": bad_key}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_agent_id(self, client: AsyncClient) -> None:
        """Missing agent_id field returns 422."""
        _, pub_b64 = generate_keypair()
        body = {"public_key": pub_b64}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_public_key(self, client: AsyncClient) -> None:
        """Missing public_key field returns 422."""
        body = {"agent_id": "good-agent"}
        response = await client.post("/agents/register", json=body)
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_requires_no_auth(self, client: AsyncClient) -> None:
        """POST /agents/register does not require authentication."""
        _, pub_b64 = generate_keypair()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        body = {"agent_id": "no-auth-agent", "public_key": pub_b64}
        # No auth headers — should still get through (422 or 201, not 401)
        response = await client.post("/agents/register", json=body)
        assert response.status_code != 401
