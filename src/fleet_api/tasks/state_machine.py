"""Task state machine — valid transitions and enforcement."""

import enum
from datetime import UTC, datetime


class TaskStatus(enum.Enum):
    """All possible task states per RFC 1 section 2."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETASKED = "retasked"
    REDIRECTED = "redirected"


TERMINAL_STATES: set[TaskStatus] = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.RETASKED,
    TaskStatus.REDIRECTED,
}

# Valid transitions: from_status -> set of to_statuses
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.ACCEPTED: {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.PAUSED,
        TaskStatus.CANCELLED,
        TaskStatus.REDIRECTED,
    },
    TaskStatus.PAUSED: {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        TaskStatus.REDIRECTED,
    },
    TaskStatus.COMPLETED: {TaskStatus.RETASKED},
    TaskStatus.FAILED: {TaskStatus.RETASKED},
}


class InvalidStateTransition(Exception):  # noqa: N818 — domain name per RFC spec
    """Raised when a task transition is not allowed."""

    def __init__(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        valid = VALID_TRANSITIONS.get(from_status, set())
        valid_str = ", ".join(s.value for s in sorted(valid, key=lambda s: s.value))
        super().__init__(
            f"Invalid state transition: {from_status.value} -> {to_status.value}. "
            f"Valid transitions from {from_status.value}: "
            f"{valid_str if valid_str else 'none (terminal state)'}"
        )


def validate_transition(from_status: TaskStatus, to_status: TaskStatus) -> None:
    """Validate that a state transition is allowed.

    Raises InvalidStateTransition if the transition is not in VALID_TRANSITIONS.
    """
    valid = VALID_TRANSITIONS.get(from_status, set())
    if to_status not in valid:
        raise InvalidStateTransition(from_status, to_status)


def is_terminal(status: TaskStatus) -> bool:
    """Return True if the status is a terminal state."""
    return status in TERMINAL_STATES


def now_utc() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(UTC)
