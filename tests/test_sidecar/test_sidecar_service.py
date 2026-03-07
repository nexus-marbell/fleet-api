"""Service-level tests for sidecar operations.

Tests the process_sidecar_event function directly (not through HTTP),
verifying task state mutations, started_at handling, and result storage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet_api.errors import (
    AuthError,
    ErrorCode,
    InputValidationError,
    NotFoundError,
)
from fleet_api.tasks.models import Task, TaskPriority, TaskStatus
from fleet_api.tasks.service import process_sidecar_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TASK_ID = "task-svc-001"
WORKFLOW_ID = "wf-review"
EXECUTOR_ID = "executor-001"
CREATED_AT = datetime(2026, 3, 7, 14, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_task(
    task_id: str = TASK_ID,
    status: TaskStatus = TaskStatus.ACCEPTED,
    executor_agent_id: str = EXECUTOR_ID,
    started_at: datetime | None = None,
    metadata_: dict[str, Any] | None = None,
) -> MagicMock:
    """Create a mock Task ORM object."""
    task = MagicMock(spec=Task)
    task.id = task_id
    task.workflow_id = WORKFLOW_ID
    task.executor_agent_id = executor_agent_id
    task.status = status
    task.principal_agent_id = "caller-agent"
    task.priority = TaskPriority.NORMAL
    task.input = {"data": "test"}
    task.result = None
    task.created_at = CREATED_AT
    task.started_at = started_at
    task.completed_at = None
    task.metadata_ = metadata_

    def mock_transition(new_status: TaskStatus) -> None:
        task.status = new_status
        if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            task.completed_at = datetime.now(UTC)

    task.transition_to = MagicMock(side_effect=mock_transition)
    return task


def _make_mock_session(
    task: MagicMock | None = None,
    last_sequence: int = 0,
) -> AsyncMock:
    """Create a mock AsyncSession."""
    session = AsyncMock()

    # session.get returns the task
    session.get = AsyncMock(return_value=task)

    # sequence query
    mock_result = MagicMock()
    mock_result.scalar_one.return_value = last_sequence
    session.execute = AsyncMock(return_value=mock_result)

    return session


# ---------------------------------------------------------------------------
# started_at set on running transition
# ---------------------------------------------------------------------------


class TestStartedAtOnRunning:
    """started_at is set when task transitions to running."""

    @pytest.mark.asyncio
    async def test_started_at_set_on_first_running(self) -> None:
        """started_at is set when a task transitions to running for the first time."""
        task = _make_mock_task(status=TaskStatus.ACCEPTED, started_at=None)
        session = _make_mock_session(task=task, last_sequence=0)

        await process_sidecar_event(
            session=session,
            task_id=TASK_ID,
            event_type="status",
            data={"status": "running", "message": "Starting"},
            sequence=1,
            executor_agent_id=EXECUTOR_ID,
        )

        assert task.started_at is not None

    @pytest.mark.asyncio
    async def test_started_at_not_overwritten(self) -> None:
        """started_at is NOT overwritten if already set (e.g. paused -> running)."""
        original_started = datetime(2026, 3, 7, 13, 0, 0, tzinfo=UTC)
        task = _make_mock_task(
            status=TaskStatus.PAUSED,
            started_at=original_started,
        )
        session = _make_mock_session(task=task, last_sequence=3)

        await process_sidecar_event(
            session=session,
            task_id=TASK_ID,
            event_type="status",
            data={"status": "running"},
            sequence=4,
            executor_agent_id=EXECUTOR_ID,
        )

        assert task.started_at == original_started


# ---------------------------------------------------------------------------
# Completed event stores result
# ---------------------------------------------------------------------------


class TestCompletedEventResult:
    """Completed event stores result and quality on the task."""

    @pytest.mark.asyncio
    async def test_result_stored(self) -> None:
        """task.result is set from completed event data."""
        task = _make_mock_task(status=TaskStatus.RUNNING)
        session = _make_mock_session(task=task, last_sequence=1)

        await process_sidecar_event(
            session=session,
            task_id=TASK_ID,
            event_type="completed",
            data={"result": {"summary": "Done"}, "quality": {"input_valid": True}},
            sequence=2,
            executor_agent_id=EXECUTOR_ID,
        )

        assert task.result == {"summary": "Done"}

    @pytest.mark.asyncio
    async def test_quality_stored_in_metadata(self) -> None:
        """Quality data from completed event is stored in metadata."""
        task = _make_mock_task(status=TaskStatus.RUNNING)
        session = _make_mock_session(task=task, last_sequence=1)

        await process_sidecar_event(
            session=session,
            task_id=TASK_ID,
            event_type="completed",
            data={
                "result": {"done": True},
                "quality": {"input_valid": True, "execution_clean": True},
                "warnings": ["minor issue"],
            },
            sequence=2,
            executor_agent_id=EXECUTOR_ID,
        )

        assert task.metadata_["quality"] == {"input_valid": True, "execution_clean": True}
        assert task.metadata_["warnings"] == ["minor issue"]


# ---------------------------------------------------------------------------
# Failed event stores error
# ---------------------------------------------------------------------------


class TestFailedEventResult:
    """Failed event stores error details in task.result."""

    @pytest.mark.asyncio
    async def test_error_stored_in_result(self) -> None:
        """task.result contains error_code and message from failed event."""
        task = _make_mock_task(status=TaskStatus.RUNNING)
        session = _make_mock_session(task=task, last_sequence=1)

        await process_sidecar_event(
            session=session,
            task_id=TASK_ID,
            event_type="failed",
            data={"error_code": "TIMEOUT", "message": "Execution timed out"},
            sequence=2,
            executor_agent_id=EXECUTOR_ID,
        )

        assert task.result == {"error_code": "TIMEOUT", "message": "Execution timed out"}


# ---------------------------------------------------------------------------
# Task not found at service level
# ---------------------------------------------------------------------------


class TestServiceTaskNotFound:
    """process_sidecar_event raises NotFoundError when task missing."""

    @pytest.mark.asyncio
    async def test_raises_not_found(self) -> None:
        """Non-existent task raises NotFoundError."""
        session = _make_mock_session(task=None, last_sequence=0)

        with pytest.raises(NotFoundError) as exc_info:
            await process_sidecar_event(
                session=session,
                task_id="task-ghost",
                event_type="heartbeat",
                data={},
                sequence=1,
                executor_agent_id=EXECUTOR_ID,
            )

        assert exc_info.value.code == ErrorCode.TASK_NOT_FOUND


# ---------------------------------------------------------------------------
# Authorization at service level
# ---------------------------------------------------------------------------


class TestServiceAuthorization:
    """process_sidecar_event rejects non-executor agents."""

    @pytest.mark.asyncio
    async def test_non_executor_raises_auth_error(self) -> None:
        """Wrong executor raises AuthError."""
        task = _make_mock_task(executor_agent_id="real-executor")
        session = _make_mock_session(task=task, last_sequence=0)

        with pytest.raises(AuthError) as exc_info:
            await process_sidecar_event(
                session=session,
                task_id=TASK_ID,
                event_type="heartbeat",
                data={},
                sequence=1,
                executor_agent_id="wrong-agent",
            )

        assert exc_info.value.code == ErrorCode.NOT_AUTHORIZED


# ---------------------------------------------------------------------------
# Sequence validation at service level
# ---------------------------------------------------------------------------


class TestServiceSequenceValidation:
    """Sequence must be strictly increasing."""

    @pytest.mark.asyncio
    async def test_duplicate_sequence_rejected(self) -> None:
        """Sequence equal to last is rejected."""
        task = _make_mock_task()
        session = _make_mock_session(task=task, last_sequence=5)

        with pytest.raises(InputValidationError) as exc_info:
            await process_sidecar_event(
                session=session,
                task_id=TASK_ID,
                event_type="heartbeat",
                data={},
                sequence=5,
                executor_agent_id=EXECUTOR_ID,
            )

        assert exc_info.value.code == ErrorCode.INVALID_INPUT

    @pytest.mark.asyncio
    async def test_lower_sequence_rejected(self) -> None:
        """Sequence less than last is rejected."""
        task = _make_mock_task()
        session = _make_mock_session(task=task, last_sequence=10)

        with pytest.raises(InputValidationError) as exc_info:
            await process_sidecar_event(
                session=session,
                task_id=TASK_ID,
                event_type="heartbeat",
                data={},
                sequence=3,
                executor_agent_id=EXECUTOR_ID,
            )

        assert exc_info.value.code == ErrorCode.INVALID_INPUT
