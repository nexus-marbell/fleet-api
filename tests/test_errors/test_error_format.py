"""Unit tests for error code registry and exception hierarchy."""

from fleet_api.errors import (
    ERROR_STATUS_CODES,
    AuthError,
    ConflictError,
    ErrorCode,
    FleetAPIError,
    InfrastructureError,
    InputValidationError,
    NotFoundError,
    StateError,
)


class TestFleetAPIErrorToDict:
    """Tests for FleetAPIError.to_dict()."""

    def test_fleet_api_error_to_dict(self) -> None:
        """All fields are present when suggestion and links are provided."""
        error = FleetAPIError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message="Workflow 'wf-test' does not exist.",
            suggestion="Check the workflow ID.",
            links={"workflows": {"href": "/workflows"}},
        )
        result = error.to_dict()

        assert result == {
            "error": True,
            "code": "WORKFLOW_NOT_FOUND",
            "message": "Workflow 'wf-test' does not exist.",
            "suggestion": "Check the workflow ID.",
            "_links": {"workflows": {"href": "/workflows"}},
        }

    def test_fleet_api_error_to_dict_minimal(self) -> None:
        """Only required fields when no suggestion or links."""
        error = FleetAPIError(
            code=ErrorCode.EXECUTION_FAILED,
            message="An internal error occurred.",
        )
        result = error.to_dict()

        assert result == {
            "error": True,
            "code": "EXECUTION_FAILED",
            "message": "An internal error occurred.",
        }
        assert "suggestion" not in result
        assert "_links" not in result


class TestErrorCodeStatusMapping:
    """Tests for error code to HTTP status mapping."""

    def test_fleet_api_error_default_status(self) -> None:
        """Each ErrorCode maps to its correct default HTTP status."""
        expected = {
            ErrorCode.ENDPOINT_NOT_FOUND: 404,
            ErrorCode.WORKFLOW_NOT_FOUND: 404,
            ErrorCode.TASK_NOT_FOUND: 404,
            ErrorCode.INVALID_SIGNATURE: 401,
            ErrorCode.AGENT_NOT_REGISTERED: 401,
            ErrorCode.TIMESTAMP_EXPIRED: 401,
            ErrorCode.NOT_AUTHORIZED: 403,
            ErrorCode.WORKFLOW_EXISTS: 409,
            ErrorCode.AGENT_EXISTS: 409,
            ErrorCode.INVALID_INPUT: 422,
            ErrorCode.IDEMPOTENCY_MISMATCH: 422,
            ErrorCode.RATE_LIMITED: 429,
            ErrorCode.EXECUTION_TIMEOUT: 504,
            ErrorCode.EXECUTION_FAILED: 500,
            ErrorCode.EXECUTOR_UNREACHABLE: 503,
            ErrorCode.AGENT_SUSPENDED: 503,
            ErrorCode.BAD_GATEWAY: 502,
            ErrorCode.DEPRECATED_PATH: 301,
            ErrorCode.INVALID_STATE_TRANSITION: 409,
            ErrorCode.TASK_NOT_PAUSABLE: 409,
            ErrorCode.TASK_NOT_PAUSED: 409,
            ErrorCode.PAUSE_TIMEOUT: 408,
            ErrorCode.CONTEXT_REJECTED: 409,
            ErrorCode.RETASK_NOT_REVIEWABLE: 409,
            ErrorCode.RETASK_DEPTH_EXCEEDED: 422,
            ErrorCode.REDIRECT_NOT_POSSIBLE: 409,
        }
        for code, status in expected.items():
            error = FleetAPIError(code=code, message="test")
            assert error.http_status == status, (
                f"{code} should map to {status}"
            )

    def test_all_error_codes_have_status(self) -> None:
        """Every ErrorCode member has an entry in ERROR_STATUS_CODES."""
        for code in ErrorCode:
            assert code in ERROR_STATUS_CODES, (
                f"{code} missing from ERROR_STATUS_CODES"
            )
        assert len(ErrorCode) == 26, (
            f"Expected 26 error codes, got {len(ErrorCode)}"
        )

    def test_custom_http_status_override(self) -> None:
        """Explicit http_status overrides the default."""
        error = FleetAPIError(
            code=ErrorCode.EXECUTION_FAILED,
            message="test",
            http_status=503,
        )
        assert error.http_status == 503


class TestErrorCodeValues:
    """Tests for ErrorCode enum values."""

    def test_error_code_values_match_names(self) -> None:
        """Each ErrorCode value matches its name."""
        for code in ErrorCode:
            assert code.value == code.name, (
                f"{code.name}.value should be '{code.name}'"
            )

    def test_error_code_is_str(self) -> None:
        """ErrorCode members are strings (StrEnum)."""
        for code in ErrorCode:
            assert isinstance(code.value, str)


class TestConvenienceSubclasses:
    """Tests for convenience exception subclasses."""

    def test_not_found_error_is_fleet_api_error(self) -> None:
        error = NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND, message="Not found"
        )
        assert isinstance(error, FleetAPIError)
        assert error.http_status == 404

    def test_auth_error_is_fleet_api_error(self) -> None:
        error = AuthError(
            code=ErrorCode.INVALID_SIGNATURE, message="Bad sig"
        )
        assert isinstance(error, FleetAPIError)
        assert error.http_status == 401

    def test_conflict_error_is_fleet_api_error(self) -> None:
        error = ConflictError(
            code=ErrorCode.WORKFLOW_EXISTS, message="Exists"
        )
        assert isinstance(error, FleetAPIError)
        assert error.http_status == 409

    def test_input_validation_error_is_fleet_api_error(self) -> None:
        error = InputValidationError(
            code=ErrorCode.INVALID_INPUT, message="Invalid"
        )
        assert isinstance(error, FleetAPIError)
        assert error.http_status == 422

    def test_state_error_is_fleet_api_error(self) -> None:
        error = StateError(
            code=ErrorCode.TASK_NOT_PAUSABLE, message="Not pausable"
        )
        assert isinstance(error, FleetAPIError)
        assert error.http_status == 409

    def test_infrastructure_error_is_fleet_api_error(self) -> None:
        error = InfrastructureError(
            code=ErrorCode.BAD_GATEWAY, message="Bad gateway"
        )
        assert isinstance(error, FleetAPIError)
        assert error.http_status == 502

    def test_subclass_to_dict_works(self) -> None:
        """Convenience subclasses inherit to_dict correctly."""
        error = NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message="Task 'abc' not found.",
            suggestion="Verify the task ID.",
        )
        result = error.to_dict()
        assert result["error"] is True
        assert result["code"] == "TASK_NOT_FOUND"
        assert result["suggestion"] == "Verify the task ID."
