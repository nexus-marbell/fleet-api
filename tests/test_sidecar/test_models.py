"""Tests for fleet_agent.models."""

from __future__ import annotations

from fleet_agent.models import PendingTask, TaskEvent


class TestPendingTask:
    """PendingTask data model."""

    def test_parses_full_payload(self) -> None:
        """All fields including optional timeout_seconds."""
        task = PendingTask(
            task_id="t-1",
            workflow_id="wf-1",
            input={"key": "value"},
            priority="normal",
            timeout_seconds=300,
            created_at="2026-03-07T12:00:00Z",
        )
        assert task.task_id == "t-1"
        assert task.timeout_seconds == 300

    def test_timeout_defaults_to_none(self) -> None:
        """timeout_seconds defaults to None when omitted."""
        task = PendingTask(
            task_id="t-2",
            workflow_id="wf-2",
            input={},
            priority="low",
            created_at="2026-03-07T12:00:00Z",
        )
        assert task.timeout_seconds is None


class TestTaskEvent:
    """TaskEvent data model."""

    def test_event_with_data(self) -> None:
        """Event carries structured data."""
        event = TaskEvent(event_type="progress", data={"percent": 50}, sequence=3)
        assert event.event_type == "progress"
        assert event.data == {"percent": 50}
        assert event.sequence == 3

    def test_event_data_defaults_to_none(self) -> None:
        """Data field defaults to None."""
        event = TaskEvent(event_type="heartbeat", sequence=1)
        assert event.data is None
