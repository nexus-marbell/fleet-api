"""Integration tests for error handling middleware."""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fleet_api.app import create_app
from fleet_api.errors import ErrorCode, FleetAPIError


@pytest.fixture
def app_with_error_routes() -> FastAPI:
    """Create app with test routes that raise specific errors."""
    app = create_app()

    @app.get("/test/fleet-error")
    async def raise_fleet_error() -> None:
        raise FleetAPIError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message="Workflow 'wf-test' does not exist.",
            suggestion="Check the workflow ID.",
            links={"workflows": {"href": "/workflows"}},
        )

    @app.get("/test/unhandled")
    async def raise_unhandled() -> None:
        raise RuntimeError("Something went wrong")

    @app.post("/test/validate")
    async def validate_input(name: int) -> dict[str, int]:
        return {"name": name}

    return app


@pytest.fixture
async def error_client(
    app_with_error_routes: FastAPI,
) -> AsyncIterator[AsyncClient]:
    """Async HTTP client with error test routes.

    Uses raise_app_exceptions=False so that FastAPI exception handlers
    process the errors and return JSON responses instead of httpx
    re-raising them.
    """
    transport = ASGITransport(
        app=app_with_error_routes, raise_app_exceptions=False
    )
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_unknown_route_returns_endpoint_not_found(
    error_client: AsyncClient,
) -> None:
    """GET /nonexistent returns 404 with ENDPOINT_NOT_FOUND error code."""
    response = await error_client.get("/nonexistent")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "ENDPOINT_NOT_FOUND"
    assert "No endpoint found at GET /nonexistent" in body["error"]["message"]


@pytest.mark.asyncio
async def test_unknown_route_has_manifest_link(
    error_client: AsyncClient,
) -> None:
    """404 response includes _links.manifest for discoverability."""
    response = await error_client.get("/nonexistent")
    body = response.json()
    assert "_links" in body["error"]
    assert body["error"]["_links"]["manifest"]["href"] == "/manifest"


@pytest.mark.asyncio
async def test_fleet_api_error_returns_json(
    error_client: AsyncClient,
) -> None:
    """FleetAPIError raised in a route returns standard JSON error format."""
    response = await error_client.get("/test/fleet-error")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert body["error"]["message"] == "Workflow 'wf-test' does not exist."
    assert body["error"]["suggestion"] == "Check the workflow ID."
    assert body["error"]["_links"]["workflows"]["href"] == "/workflows"


@pytest.mark.asyncio
async def test_unhandled_exception_returns_500(
    error_client: AsyncClient,
) -> None:
    """Unhandled RuntimeError returns 500 EXECUTION_FAILED, no stack trace."""
    response = await error_client.get("/test/unhandled")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "EXECUTION_FAILED"
    assert body["error"]["message"] == "An internal error occurred."
    # No stack trace in response
    response_text = response.text
    assert "Traceback" not in response_text
    assert "RuntimeError" not in response_text


@pytest.mark.asyncio
async def test_all_error_responses_are_json(
    error_client: AsyncClient,
) -> None:
    """All error responses have Content-Type: application/json."""
    # 404
    r404 = await error_client.get("/nonexistent")
    assert r404.headers["content-type"] == "application/json"

    # FleetAPIError
    r_fleet = await error_client.get("/test/fleet-error")
    assert r_fleet.headers["content-type"] == "application/json"

    # 500
    r500 = await error_client.get("/test/unhandled")
    assert r500.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_validation_error_returns_422(
    error_client: AsyncClient,
) -> None:
    """Invalid request body returns 422 INVALID_INPUT with details."""
    response = await error_client.post(
        "/test/validate",
        params={"name": "not-an-int"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_INPUT"
    assert body["error"]["message"] == "Request validation failed."
    assert "validation_errors" in body["error"]
    assert len(body["error"]["validation_errors"]) > 0


@pytest.mark.asyncio
async def test_suggestion_present_in_error_responses(
    error_client: AsyncClient,
) -> None:
    """Error responses include suggestion field when applicable."""
    # 404 unknown route
    r404 = await error_client.get("/nonexistent")
    body = r404.json()
    assert "suggestion" in body["error"]
    assert "manifest" in body["error"]["suggestion"].lower()

    # 500 unhandled
    r500 = await error_client.get("/test/unhandled")
    body = r500.json()
    assert "suggestion" in body["error"]
