"""Shared test fixtures."""

import pytest
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app


@pytest.fixture
def app():
    """Create a test application instance."""
    return create_app()


@pytest.fixture
async def client(app):
    """Async HTTP client for testing."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
