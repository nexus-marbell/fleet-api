"""Fleet API error codes and exception hierarchy."""

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """All 24 fleet-api error codes from RFC 1 S8."""

    # Routing
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"

    # Resource
    WORKFLOW_NOT_FOUND = "WORKFLOW_NOT_FOUND"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"

    # Auth
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    AGENT_NOT_REGISTERED = "AGENT_NOT_REGISTERED"
    TIMESTAMP_EXPIRED = "TIMESTAMP_EXPIRED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"

    # Conflict
    WORKFLOW_EXISTS = "WORKFLOW_EXISTS"

    # Validation
    INVALID_INPUT = "INVALID_INPUT"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"

    # Throttling
    RATE_LIMITED = "RATE_LIMITED"

    # Execution
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_FAILED = "EXECUTION_FAILED"

    # Connectivity
    EXECUTOR_UNREACHABLE = "EXECUTOR_UNREACHABLE"
    AGENT_SUSPENDED = "AGENT_SUSPENDED"

    # Infrastructure
    BAD_GATEWAY = "BAD_GATEWAY"

    # Versioning
    DEPRECATED_PATH = "DEPRECATED_PATH"

    # State
    TASK_NOT_PAUSABLE = "TASK_NOT_PAUSABLE"
    TASK_NOT_PAUSED = "TASK_NOT_PAUSED"
    PAUSE_TIMEOUT = "PAUSE_TIMEOUT"
    CONTEXT_REJECTED = "CONTEXT_REJECTED"
    RETASK_NOT_REVIEWABLE = "RETASK_NOT_REVIEWABLE"
    RETASK_DEPTH_EXCEEDED = "RETASK_DEPTH_EXCEEDED"
    REDIRECT_NOT_POSSIBLE = "REDIRECT_NOT_POSSIBLE"


# Default HTTP status codes for each error
ERROR_STATUS_CODES: dict[ErrorCode, int] = {
    ErrorCode.ENDPOINT_NOT_FOUND: 404,
    ErrorCode.WORKFLOW_NOT_FOUND: 404,
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.INVALID_SIGNATURE: 401,
    ErrorCode.AGENT_NOT_REGISTERED: 401,
    ErrorCode.TIMESTAMP_EXPIRED: 401,
    ErrorCode.NOT_AUTHORIZED: 403,
    ErrorCode.WORKFLOW_EXISTS: 409,
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.IDEMPOTENCY_MISMATCH: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.EXECUTION_TIMEOUT: 504,
    ErrorCode.EXECUTION_FAILED: 500,
    ErrorCode.EXECUTOR_UNREACHABLE: 503,
    ErrorCode.AGENT_SUSPENDED: 503,
    ErrorCode.BAD_GATEWAY: 502,
    ErrorCode.DEPRECATED_PATH: 301,
    ErrorCode.TASK_NOT_PAUSABLE: 409,
    ErrorCode.TASK_NOT_PAUSED: 409,
    ErrorCode.PAUSE_TIMEOUT: 408,
    ErrorCode.CONTEXT_REJECTED: 409,
    ErrorCode.RETASK_NOT_REVIEWABLE: 409,
    ErrorCode.RETASK_DEPTH_EXCEEDED: 422,
    ErrorCode.REDIRECT_NOT_POSSIBLE: 409,
}


class FleetAPIError(Exception):
    """Base exception for all fleet-api errors."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        suggestion: str | None = None,
        links: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.links = links or {}
        self.http_status = http_status or ERROR_STATUS_CODES[code]
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Convert to the flat error response format (RFC 1 S3.3/S3.4/S8)."""
        result: dict[str, Any] = {
            "error": True,
            "code": self.code.value,
            "message": self.message,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        if self.links:
            result["_links"] = self.links
        return result


# Convenience subclasses
class NotFoundError(FleetAPIError):
    """404 errors."""


class AuthError(FleetAPIError):
    """401/403 errors."""


class ConflictError(FleetAPIError):
    """409 errors."""


class InputValidationError(FleetAPIError):
    """422 errors."""


class StateError(FleetAPIError):
    """State machine violation errors (409)."""


class InfrastructureError(FleetAPIError):
    """502/503/504 errors."""
