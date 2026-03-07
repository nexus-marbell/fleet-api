"""Standardized error handling middleware."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fleet_api.errors import ErrorCode, FleetAPIError

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    """Register all error handlers on the FastAPI app."""

    @app.exception_handler(FleetAPIError)
    async def fleet_api_error_handler(
        request: Request, exc: FleetAPIError
    ) -> JSONResponse:
        """Handle FleetAPIError exceptions."""
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_dict(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Handle standard HTTP exceptions in our error format."""
        if exc.status_code == 404:
            error = FleetAPIError(
                code=ErrorCode.ENDPOINT_NOT_FOUND,
                message=(
                    f"No endpoint found at {request.method} {request.url.path}"
                ),
                suggestion=(
                    "Check the URL and method. "
                    "Use GET /manifest to discover endpoints."
                ),
                links={"manifest": {"href": "/manifest"}},
            )
            return JSONResponse(status_code=404, content=error.to_dict())

        # For other HTTP exceptions, wrap in flat error format
        detail = str(exc.detail) if exc.detail else f"HTTP {exc.status_code}"
        content: dict[str, Any] = {
            "error": True,
            "code": f"HTTP_{exc.status_code}",
            "message": detail,
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle Pydantic/FastAPI validation errors in our error format."""
        error = FleetAPIError(
            code=ErrorCode.INVALID_INPUT,
            message="Request validation failed.",
            suggestion="Check the request body against the expected schema.",
        )
        response = error.to_dict()
        response["validation_errors"] = exc.errors()
        return JSONResponse(status_code=422, content=response)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Catch-all for unhandled exceptions. No stack traces in response."""
        logger.exception("Unhandled exception: %s", exc)
        error = FleetAPIError(
            code=ErrorCode.EXECUTION_FAILED,
            message="An internal error occurred.",
            suggestion="This is a transient error. Retry after a short delay.",
        )
        return JSONResponse(status_code=500, content=error.to_dict())
