"""Tests for the task state machine — pure Python, no database required."""

import pytest

from fleet_api.tasks.models import Task
from fleet_api.tasks.state_machine import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidStateTransition,
    TaskStatus,
    is_terminal,
    validate_transition,
)


class TestValidTransitions:
    """All 13 valid transitions must pass without raising."""

    # accepted ->
    def test_accepted_to_running(self) -> None:
        validate_transition(TaskStatus.ACCEPTED, TaskStatus.RUNNING)

    def test_accepted_to_cancelled(self) -> None:
        validate_transition(TaskStatus.ACCEPTED, TaskStatus.CANCELLED)

    def test_accepted_to_failed(self) -> None:
        validate_transition(TaskStatus.ACCEPTED, TaskStatus.FAILED)

    # running ->
    def test_running_to_completed(self) -> None:
        validate_transition(TaskStatus.RUNNING, TaskStatus.COMPLETED)

    def test_running_to_failed(self) -> None:
        validate_transition(TaskStatus.RUNNING, TaskStatus.FAILED)

    def test_running_to_paused(self) -> None:
        validate_transition(TaskStatus.RUNNING, TaskStatus.PAUSED)

    def test_running_to_cancelled(self) -> None:
        validate_transition(TaskStatus.RUNNING, TaskStatus.CANCELLED)

    def test_running_to_redirected(self) -> None:
        validate_transition(TaskStatus.RUNNING, TaskStatus.REDIRECTED)

    # paused ->
    def test_paused_to_running(self) -> None:
        validate_transition(TaskStatus.PAUSED, TaskStatus.RUNNING)

    def test_paused_to_cancelled(self) -> None:
        validate_transition(TaskStatus.PAUSED, TaskStatus.CANCELLED)

    def test_paused_to_redirected(self) -> None:
        validate_transition(TaskStatus.PAUSED, TaskStatus.REDIRECTED)

    # completed/failed -> retasked
    def test_completed_to_retasked(self) -> None:
        validate_transition(TaskStatus.COMPLETED, TaskStatus.RETASKED)

    def test_failed_to_retasked(self) -> None:
        validate_transition(TaskStatus.FAILED, TaskStatus.RETASKED)


class TestInvalidTransitions:
    """Invalid transitions must raise InvalidStateTransition."""

    def test_completed_to_running(self) -> None:
        with pytest.raises(InvalidStateTransition) as exc_info:
            validate_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)
        assert exc_info.value.from_status == TaskStatus.COMPLETED
        assert exc_info.value.to_status == TaskStatus.RUNNING

    def test_cancelled_to_running(self) -> None:
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.CANCELLED, TaskStatus.RUNNING)

    def test_retasked_to_running(self) -> None:
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.RETASKED, TaskStatus.RUNNING)

    def test_redirected_to_running(self) -> None:
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.REDIRECTED, TaskStatus.RUNNING)

    def test_failed_to_running(self) -> None:
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.FAILED, TaskStatus.RUNNING)

    def test_accepted_to_completed(self) -> None:
        """Cannot skip running and go straight to completed."""
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.ACCEPTED, TaskStatus.COMPLETED)

    def test_paused_to_completed(self) -> None:
        """Paused must resume (running) before completing."""
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.PAUSED, TaskStatus.COMPLETED)

    def test_completed_to_completed(self) -> None:
        """No self-transition on completed."""
        with pytest.raises(InvalidStateTransition):
            validate_transition(TaskStatus.COMPLETED, TaskStatus.COMPLETED)

    def test_error_message_includes_valid_transitions(self) -> None:
        """Error message should tell the caller what IS valid."""
        with pytest.raises(InvalidStateTransition, match="Valid transitions from completed"):
            validate_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)

    def test_terminal_state_with_no_valid_transitions(self) -> None:
        """Terminal states with no outgoing edges (cancelled, retasked, redirected)."""
        with pytest.raises(InvalidStateTransition, match="none \\(terminal state\\)"):
            validate_transition(TaskStatus.CANCELLED, TaskStatus.ACCEPTED)


class TestTerminalStates:
    """Terminal state identification."""

    def test_completed_is_terminal(self) -> None:
        assert is_terminal(TaskStatus.COMPLETED)

    def test_failed_is_terminal(self) -> None:
        assert is_terminal(TaskStatus.FAILED)

    def test_cancelled_is_terminal(self) -> None:
        assert is_terminal(TaskStatus.CANCELLED)

    def test_retasked_is_terminal(self) -> None:
        assert is_terminal(TaskStatus.RETASKED)

    def test_redirected_is_terminal(self) -> None:
        assert is_terminal(TaskStatus.REDIRECTED)

    def test_accepted_is_not_terminal(self) -> None:
        assert not is_terminal(TaskStatus.ACCEPTED)

    def test_running_is_not_terminal(self) -> None:
        assert not is_terminal(TaskStatus.RUNNING)

    def test_paused_is_not_terminal(self) -> None:
        assert not is_terminal(TaskStatus.PAUSED)

    def test_terminal_states_count(self) -> None:
        """Exactly 5 terminal states."""
        assert len(TERMINAL_STATES) == 5

    def test_all_statuses_accounted_for(self) -> None:
        """Every TaskStatus either has valid transitions or is terminal with no exits."""
        for status in TaskStatus:
            has_transitions = status in VALID_TRANSITIONS and len(VALID_TRANSITIONS[status]) > 0
            is_term = is_terminal(status)
            # Every status must either have outgoing transitions OR be a terminal state
            assert has_transitions or is_term, (
                f"{status} has no transitions and is not terminal"
            )


class TestTransitionCount:
    """Verify the expected number of valid transitions."""

    def test_total_valid_transitions(self) -> None:
        """13 valid transitions total."""
        total = sum(len(targets) for targets in VALID_TRANSITIONS.values())
        assert total == 13


class TestTaskTransitionTo:
    """Test the Task.transition_to method (model-level, no DB)."""

    def _make_task(self, status: TaskStatus) -> Task:
        """Create a minimal Task instance for testing state transitions."""
        task = Task(
            id="test-task",
            workflow_id="test-workflow",
            principal_agent_id="test-agent",
            status=status,
            input={"test": True},
        )
        task.completed_at = None
        return task

    def test_transition_to_running(self) -> None:
        task = self._make_task(TaskStatus.ACCEPTED)
        task.transition_to(TaskStatus.RUNNING)
        assert task.status == TaskStatus.RUNNING
        assert task.completed_at is None

    def test_transition_to_completed_sets_completed_at(self) -> None:
        task = self._make_task(TaskStatus.RUNNING)
        task.transition_to(TaskStatus.COMPLETED)
        assert task.status == TaskStatus.COMPLETED
        assert task.completed_at is not None

    def test_transition_to_failed_sets_completed_at(self) -> None:
        task = self._make_task(TaskStatus.RUNNING)
        task.transition_to(TaskStatus.FAILED)
        assert task.status == TaskStatus.FAILED
        assert task.completed_at is not None

    def test_transition_to_cancelled_sets_completed_at(self) -> None:
        task = self._make_task(TaskStatus.RUNNING)
        task.transition_to(TaskStatus.CANCELLED)
        assert task.status == TaskStatus.CANCELLED
        assert task.completed_at is not None

    def test_transition_to_retasked_sets_completed_at(self) -> None:
        task = self._make_task(TaskStatus.COMPLETED)
        task.transition_to(TaskStatus.RETASKED)
        assert task.status == TaskStatus.RETASKED
        assert task.completed_at is not None

    def test_transition_to_redirected_sets_completed_at(self) -> None:
        task = self._make_task(TaskStatus.RUNNING)
        task.transition_to(TaskStatus.REDIRECTED)
        assert task.status == TaskStatus.REDIRECTED
        assert task.completed_at is not None

    def test_invalid_transition_raises(self) -> None:
        task = self._make_task(TaskStatus.COMPLETED)
        with pytest.raises(InvalidStateTransition):
            task.transition_to(TaskStatus.RUNNING)
        # Status should NOT have changed
        assert task.status == TaskStatus.COMPLETED

    def test_non_terminal_transition_no_completed_at(self) -> None:
        """Pausing a task should not set completed_at."""
        task = self._make_task(TaskStatus.RUNNING)
        task.transition_to(TaskStatus.PAUSED)
        assert task.status == TaskStatus.PAUSED
        assert task.completed_at is None
