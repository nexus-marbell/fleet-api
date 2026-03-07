"""Structural tests — app starts, basic routes work."""

import pytest


@pytest.mark.asyncio
async def test_root_returns_ok(client):
    """GET / returns status ok."""
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_app_has_openapi(client):
    """OpenAPI schema is generated."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "Fleet API"
