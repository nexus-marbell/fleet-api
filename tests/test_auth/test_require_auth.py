"""Tests for the require_auth FastAPI dependency.

Uses a mock AgentLookup injected via FastAPI dependency overrides so
these tests have no database dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from fleet_api.middleware.auth import (
    AuthenticatedAgent,
    get_agent_lookup,
    require_auth,
)
from tests.auth_helpers import generate_test_keypair, sign_request

# ---------------------------------------------------------------------------
# Mock agent lookup
# ---------------------------------------------------------------------------


class MockAgentLookup:
    """In-memory agent store for tests."""

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._suspended: set[str] = set()

    def register(self, agent_id: str, public_key: Ed25519PublicKey) -> None:
        self._keys[agent_id] = public_key

    def suspend(self, agent_id: str) -> None:
        self._suspended.add(agent_id)

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        return self._keys.get(agent_id)

    async def is_agent_suspended(self, agent_id: str) -> bool:
        return agent_id in self._suspended


# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _create_test_app(mock_lookup: MockAgentLookup) -> FastAPI:
    """Minimal FastAPI app with require_auth wired."""
    app = FastAPI()

    app.dependency_overrides[get_agent_lookup] = lambda: mock_lookup

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get("/manifest")
    async def manifest() -> dict[str, str]:
        return {"name": "fleet-api"}

    @app.post("/agents/register")
    async def register() -> dict[str, str]:
        return {"registered": "true"}

    @app.get("/openapi.json")
    async def openapi_json() -> dict[str, str]:
        return {"openapi": "3.1.0"}

    @app.get("/protected")
    async def protected(
        agent: AuthenticatedAgent | None = Depends(require_auth),
    ) -> dict[str, str]:
        assert agent is not None
        return {"agent_id": agent.agent_id}

    @app.post("/protected-post")
    async def protected_post(
        agent: AuthenticatedAgent | None = Depends(require_auth),
    ) -> dict[str, str]:
        assert agent is not None
        return {"agent_id": agent.agent_id}

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_lookup() -> MockAgentLookup:
    return MockAgentLookup()


@pytest.fixture
def private_key_and_id(mock_lookup: MockAgentLookup) -> tuple:
    """Register a test agent and return (private_key, agent_id)."""
    private_key, _ = generate_test_keypair()
    agent_id = "test-agent-001"
    mock_lookup.register(agent_id, private_key.public_key())
    return private_key, agent_id


@pytest.fixture
async def client(mock_lookup: MockAgentLookup) -> AsyncClient:
    app = _create_test_app(mock_lookup)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRequireAuthHappyPath:
    @pytest.mark.asyncio
    async def test_valid_signature_returns_agent(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Valid signature returns the authenticated agent_id."""
        private_key, agent_id = private_key_and_id
        headers = sign_request("GET", "/protected", None, private_key, agent_id)

        response = await client.get("/protected", headers=headers)

        assert response.status_code == 200
        assert response.json() == {"agent_id": agent_id}

    @pytest.mark.asyncio
    async def test_valid_post_with_body(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """POST with a body is signed and verified correctly."""
        private_key, agent_id = private_key_and_id
        body = b'{"payload": "test"}'
        headers = sign_request("POST", "/protected-post", body, private_key, agent_id)
        headers["Content-Type"] = "application/json"

        response = await client.post("/protected-post", content=body, headers=headers)

        assert response.status_code == 200
        assert response.json() == {"agent_id": agent_id}


# ---------------------------------------------------------------------------
# Unprotected paths
# ---------------------------------------------------------------------------


class TestUnprotectedPaths:
    @pytest.mark.asyncio
    async def test_root_skips_auth(self, client: AsyncClient) -> None:
        """GET / does not require authentication."""
        response = await client.get("/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_skips_auth(self, client: AsyncClient) -> None:
        """GET /health does not require authentication."""
        response = await client.get("/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_manifest_skips_auth(self, client: AsyncClient) -> None:
        """GET /manifest does not require authentication."""
        response = await client.get("/manifest")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_register_skips_auth(self, client: AsyncClient) -> None:
        """POST /agents/register does not require authentication."""
        response = await client.post("/agents/register")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_openapi_json_skips_auth(self, client: AsyncClient) -> None:
        """GET /openapi.json does not require authentication (RFC §4.4)."""
        response = await client.get("/openapi.json")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestRequireAuthErrors:
    @pytest.mark.asyncio
    async def test_missing_authorization_header(self, client: AsyncClient) -> None:
        """Missing Authorization header returns 401 INVALID_SIGNATURE."""
        response = await client.get("/protected")
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_malformed_authorization_header(self, client: AsyncClient) -> None:
        """Malformed Authorization header returns 401 INVALID_SIGNATURE."""
        headers = {
            "Authorization": "Bearer not-a-signature",
            "X-Fleet-Timestamp": datetime.now(UTC).isoformat(),
        }
        response = await client.get("/protected", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_missing_timestamp(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Missing X-Fleet-Timestamp returns 401 INVALID_SIGNATURE (malformed request)."""
        private_key, agent_id = private_key_and_id
        headers = sign_request("GET", "/protected", None, private_key, agent_id)
        del headers["X-Fleet-Timestamp"]

        response = await client.get("/protected", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_expired_timestamp(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Timestamp 10 minutes old returns 401 TIMESTAMP_EXPIRED."""
        private_key, agent_id = private_key_and_id
        old_time = datetime.now(UTC) - timedelta(minutes=10)
        headers = sign_request(
            "GET", "/protected", None, private_key, agent_id, timestamp=old_time
        )

        response = await client.get("/protected", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "TIMESTAMP_EXPIRED"

    @pytest.mark.asyncio
    async def test_unknown_agent(self, client: AsyncClient) -> None:
        """Unknown agent_id returns 401 AGENT_NOT_REGISTERED."""
        private_key, _ = generate_test_keypair()
        headers = sign_request("GET", "/protected", None, private_key, "ghost-agent")

        response = await client.get("/protected", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "AGENT_NOT_REGISTERED"

    @pytest.mark.asyncio
    async def test_suspended_agent(
        self,
        client: AsyncClient,
        private_key_and_id: tuple,
        mock_lookup: MockAgentLookup,
    ) -> None:
        """Suspended agent returns 403 NOT_AUTHORIZED."""
        private_key, agent_id = private_key_and_id
        mock_lookup.suspend(agent_id)
        headers = sign_request("GET", "/protected", None, private_key, agent_id)

        response = await client.get("/protected", headers=headers)
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "NOT_AUTHORIZED"

    @pytest.mark.asyncio
    async def test_invalid_signature(
        self, client: AsyncClient, mock_lookup: MockAgentLookup
    ) -> None:
        """Signature from a different key returns 401 INVALID_SIGNATURE."""
        # Register agent with one key
        real_key, _ = generate_test_keypair()
        agent_id = "wrong-key-agent"
        mock_lookup.register(agent_id, real_key.public_key())

        # Sign with a different key
        imposter_key, _ = generate_test_keypair()
        headers = sign_request("GET", "/protected", None, imposter_key, agent_id)

        response = await client.get("/protected", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"


# ---------------------------------------------------------------------------
# Placeholder lookup (Minor 2)
# ---------------------------------------------------------------------------


class TestPlaceholderAgentLookup:
    @pytest.mark.asyncio
    async def test_placeholder_returns_503(self) -> None:
        """PlaceholderAgentLookup produces 503 SERVICE_UNAVAILABLE."""
        app = FastAPI()

        # Use the default get_agent_lookup (returns PlaceholderAgentLookup)
        @app.get("/protected")
        async def protected(
            agent: AuthenticatedAgent | None = Depends(require_auth),
        ) -> dict[str, str]:
            assert agent is not None
            return {"agent_id": agent.agent_id}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            private_key, _ = generate_test_keypair()
            headers = sign_request("GET", "/protected", None, private_key, "any-agent")

            response = await client.get("/protected", headers=headers)
            assert response.status_code == 503
            detail = response.json()["detail"]
            assert detail["error"]["code"] == "SERVICE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Query string signing (Minor 3 / Blocker 1 regression guard)
# ---------------------------------------------------------------------------


class TestQueryStringSigning:
    @pytest.mark.asyncio
    async def test_query_params_included_in_signature(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Query string is included in the signing string (RFC §4.2)."""
        private_key, agent_id = private_key_and_id
        path_with_query = "/protected?filter=active&page=1"
        headers = sign_request("GET", path_with_query, None, private_key, agent_id)

        response = await client.get(path_with_query, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"agent_id": agent_id}

    @pytest.mark.asyncio
    async def test_altered_query_params_break_signature(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Altering query params after signing invalidates the signature."""
        private_key, agent_id = private_key_and_id
        # Sign over one query string
        headers = sign_request(
            "GET", "/protected?status=active", None, private_key, agent_id
        )
        # Send with different query params (MITM tampering scenario)
        response = await client.get("/protected?status=deleted", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_added_query_params_break_signature(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Adding query params to a request signed without them breaks verification."""
        private_key, agent_id = private_key_and_id
        # Sign without query params
        headers = sign_request("GET", "/protected", None, private_key, agent_id)
        # Send with query params appended
        response = await client.get("/protected?injected=true", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"

    @pytest.mark.asyncio
    async def test_removed_query_params_break_signature(
        self, client: AsyncClient, private_key_and_id: tuple
    ) -> None:
        """Removing query params from a signed request breaks verification."""
        private_key, agent_id = private_key_and_id
        # Sign with query params
        headers = sign_request(
            "GET", "/protected?important=yes", None, private_key, agent_id
        )
        # Send without query params (stripped by MITM)
        response = await client.get("/protected", headers=headers)
        assert response.status_code == 401
        detail = response.json()["detail"]
        assert detail["error"]["code"] == "INVALID_SIGNATURE"
