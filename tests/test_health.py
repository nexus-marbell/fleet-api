"""Tests for GET /health endpoint."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.database.connection import get_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session_operational() -> AsyncMock:
    """Return a mock session where SELECT 1 succeeds."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    return session


def _mock_session_broken(error_msg: str = "connection refused") -> AsyncMock:
    """Return a mock session where SELECT 1 raises."""
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=Exception(error_msg))
    return session


async def _override_session_operational():  # type: ignore[no-untyped-def]
    yield _mock_session_operational()


async def _override_session_broken():  # type: ignore[no-untyped-def]
    yield _mock_session_broken()


def _mock_session_timeout() -> AsyncMock:
    """Return a mock session where SELECT 1 hangs until cancelled."""
    session = AsyncMock()

    async def _hang(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(60)  # will be cancelled by wait_for timeout

    session.execute = AsyncMock(side_effect=_hang)
    return session


async def _override_session_timeout():  # type: ignore[no-untyped-def]
    yield _mock_session_timeout()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def healthy_client():
    """Client with a healthy (mocked) database."""
    app = create_app()
    app.dependency_overrides[get_session] = _override_session_operational
    return app


@pytest.fixture
def unhealthy_client():
    """Client with a broken (mocked) database."""
    app = create_app()
    app.dependency_overrides[get_session] = _override_session_broken
    return app


@pytest.fixture
def timeout_client():
    """Client with a database that times out."""
    app = create_app()
    app.dependency_overrides[get_session] = _override_session_timeout
    return app


# ---------------------------------------------------------------------------
# Tests: healthy database -> 200
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_operational(healthy_client: Any) -> None:
    """Healthy database returns 200 with operational status."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "operational"
    assert body["components"]["database"]["status"] == "operational"


@pytest.mark.asyncio
async def test_health_contains_all_required_fields(healthy_client: Any) -> None:
    """Response includes every required top-level field."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    body = resp.json()
    required = {"status", "checked_at", "uptime_seconds", "version", "components", "_links"}
    assert required.issubset(body.keys()), f"Missing fields: {required - body.keys()}"


@pytest.mark.asyncio
async def test_health_links(healthy_client: Any) -> None:
    """_links contains self, manifest, and status_page per RFC §3.8."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    links = resp.json()["_links"]
    assert links["self"] == "/health"
    assert links["manifest"] == "/manifest"
    assert links["status_page"] == "/status"


@pytest.mark.asyncio
async def test_health_uptime_positive(healthy_client: Any) -> None:
    """Uptime is a non-negative integer."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    uptime = resp.json()["uptime_seconds"]
    assert isinstance(uptime, int)
    assert uptime >= 0


@pytest.mark.asyncio
async def test_health_version_present(healthy_client: Any) -> None:
    """Version field is a non-empty string."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    version = resp.json()["version"]
    assert isinstance(version, str)
    assert len(version) > 0


@pytest.mark.asyncio
async def test_health_database_latency(healthy_client: Any) -> None:
    """Operational database component includes latency_ms and last_successful_query."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    db = resp.json()["components"]["database"]
    assert "latency_ms" in db
    assert isinstance(db["latency_ms"], int)
    assert db["latency_ms"] >= 0
    assert "last_successful_query" in db


@pytest.mark.asyncio
async def test_health_content_type_json(healthy_client: Any) -> None:
    """Content-Type is application/json even on success."""
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert "application/json" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# Tests: broken database -> 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_unhealthy_database(unhealthy_client: Any) -> None:
    """Database failure returns 503 with unhealthy status."""
    transport = ASGITransport(app=unhealthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["database"]["status"] == "unhealthy"
    assert "error" in body["components"]["database"]


@pytest.mark.asyncio
async def test_health_unhealthy_content_type_json(unhealthy_client: Any) -> None:
    """Content-Type is application/json even on 503."""
    transport = ASGITransport(app=unhealthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 503
    assert "application/json" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_health_unhealthy_still_has_all_fields(unhealthy_client: Any) -> None:
    """503 response still includes all required top-level fields."""
    transport = ASGITransport(app=unhealthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    body = resp.json()
    required = {"status", "checked_at", "uptime_seconds", "version", "components", "_links"}
    assert required.issubset(body.keys()), f"Missing fields: {required - body.keys()}"


# ---------------------------------------------------------------------------
# Tests: no authentication required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_no_auth_required(healthy_client: Any) -> None:
    """Health endpoint is accessible without auth headers.

    Validates that a request with no Authorization header receives a 200,
    confirming the endpoint is reachable by unauthenticated clients (e.g.
    Docker healthchecks, load balancers). Does NOT test the UNPROTECTED_PATHS
    bypass mechanism in auth middleware — that is covered by auth tests.
    """
    transport = ASGITransport(app=healthy_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    # No Authorization header sent — should still succeed
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: database timeout -> 503 degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_database_timeout(timeout_client: Any) -> None:
    """Database timeout returns 503 with degraded status and latency_ms."""
    transport = ASGITransport(app=timeout_client)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    db = body["components"]["database"]
    assert db["status"] == "degraded"
    assert db["error"] == "timeout"
    assert "latency_ms" in db
    assert isinstance(db["latency_ms"], int)
