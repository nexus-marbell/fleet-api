"""Task response builders — HATEOAS links, detail/summary serializers, cursor pagination."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from fleet_api.errors import ErrorCode, InputValidationError
from fleet_api.tasks.models import Task, TaskStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IDEMPOTENCY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Status -> links reference table (RFC section 3.6)
# ---------------------------------------------------------------------------
#
# Single source of truth for which action links appear per task status.
# Every status also gets: self, workflow, stream (unconditionally).
# Action links use {"method": "POST", "href": "..."} per RFC.
#
# Reference:
#   accepted   -> cancel
#   running    -> cancel, pause, context, redirect
#   paused     -> resume, cancel, context, redirect
#   completed  -> retask, rerun
#   failed     -> retask, rerun
#   cancelled  -> rerun
#   retasked   -> (none)
#   redirected -> (none)

_STATUS_ACTION_LINKS: dict[TaskStatus, tuple[str, ...]] = {
    TaskStatus.ACCEPTED: ("cancel",),
    TaskStatus.RUNNING: ("cancel", "pause", "context", "redirect"),
    TaskStatus.PAUSED: ("resume", "cancel", "context", "redirect"),
    TaskStatus.COMPLETED: ("retask", "rerun"),
    TaskStatus.FAILED: ("retask", "rerun"),
    TaskStatus.CANCELLED: ("rerun",),
    TaskStatus.RETASKED: (),
    TaskStatus.REDIRECTED: (),
}

# Path suffix for each action link (relative to task base path).
# "rerun" is special — it points to /workflows/{wf}/run, handled separately.
_ACTION_LINK_SUFFIX: dict[str, str] = {
    "cancel": "/cancel",
    "pause": "/pause",
    "resume": "/resume",
    "context": "/context",
    "redirect": "/redirect",
    "retask": "/retask",
    # "rerun" handled in build_task_links (different base path)
}


# ---------------------------------------------------------------------------
# HATEOAS link builder (shared across task endpoints)
# ---------------------------------------------------------------------------


def build_task_links(task_id: str, workflow_id: str, status: TaskStatus | str) -> dict[str, Any]:
    """Build state-dependent HATEOAS links for a task (RFC section 3.6).

    Uses the _STATUS_ACTION_LINKS reference table as the single source of
    truth. Non-action links (self, workflow, stream) are always present.
    Action links include ``method: "POST"`` per RFC.
    """
    if not isinstance(status, TaskStatus):
        status = TaskStatus(status)

    base = f"/workflows/{workflow_id}/tasks/{task_id}"
    links: dict[str, Any] = {
        "self": {"href": base},
        "workflow": {"href": f"/workflows/{workflow_id}"},
        "stream": {"href": f"{base}/stream"},
    }

    for action in _STATUS_ACTION_LINKS.get(status, ()):
        if action == "rerun":
            links["rerun"] = {"method": "POST", "href": f"/workflows/{workflow_id}/run"}
        else:
            links[action] = {"method": "POST", "href": f"{base}{_ACTION_LINK_SUFFIX[action]}"}

    return links


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _compute_duration(started_at: datetime | None, completed_at: datetime | None) -> int | None:
    """Compute duration in seconds between started_at and completed_at."""
    if started_at is None or completed_at is None:
        return None
    return int((completed_at - started_at).total_seconds())


def task_to_detail_response(task: Task) -> dict[str, Any]:
    """Convert a Task model to a full detail response dict (RFC section 3.6)."""
    status = task.status if isinstance(task.status, TaskStatus) else TaskStatus(task.status)

    response: dict[str, Any] = {
        "task_id": task.id,
        "workflow_id": task.workflow_id,
        "status": status.value,
        "caller": task.principal_agent_id,
        "executor": task.executor_agent_id,
        "priority": (
            task.priority.value if hasattr(task.priority, "value") else str(task.priority)
        ),
        "input": task.input,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

    if task.started_at is not None:
        response["started_at"] = task.started_at.isoformat()

    if status == TaskStatus.RUNNING:
        response["progress"] = task.metadata_.get("progress", 0) if task.metadata_ else 0
        response["estimated_completion"] = (
            task.metadata_.get("estimated_completion") if task.metadata_ else None
        )
    elif status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        response["result"] = task.result
        response["warnings"] = task.metadata_.get("warnings", []) if task.metadata_ else []
        if status == TaskStatus.COMPLETED:
            # Intentional: quality defaults to all-true when metadata is absent.
            # This is the happy-path assumption — callers that need to signal
            # degraded quality must explicitly set quality flags in task metadata.
            response["quality"] = (
                task.metadata_.get(
                    "quality",
                    {"input_valid": True, "execution_clean": True, "result_complete": True},
                )
                if task.metadata_
                else {"input_valid": True, "execution_clean": True, "result_complete": True}
            )
        response["completed_at"] = task.completed_at.isoformat() if task.completed_at else None
        response["duration_seconds"] = _compute_duration(task.started_at, task.completed_at)

    elif status in (TaskStatus.CANCELLED, TaskStatus.REDIRECTED, TaskStatus.RETASKED):
        if task.completed_at is not None:
            response["completed_at"] = task.completed_at.isoformat()

    response["_links"] = build_task_links(task.id, task.workflow_id, status)
    return response


def task_to_summary_response(task: Task) -> dict[str, Any]:
    """Convert a Task model to a list summary response dict (RFC section 3.7)."""
    status = task.status if isinstance(task.status, TaskStatus) else TaskStatus(task.status)

    response: dict[str, Any] = {
        "task_id": task.id,
        "status": status.value,
        "caller": task.principal_agent_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }

    if task.completed_at is not None:
        response["completed_at"] = task.completed_at.isoformat()

    if status == TaskStatus.COMPLETED:
        response["duration_seconds"] = _compute_duration(task.started_at, task.completed_at)

    base = f"/workflows/{task.workflow_id}/tasks/{task.id}"
    response["_links"] = {
        "self": {"href": base},
        "stream": {"href": f"{base}/stream"},
    }
    return response


# ---------------------------------------------------------------------------
# Cursor pagination helpers
# ---------------------------------------------------------------------------


def encode_task_cursor(task_id: str, created_at: datetime) -> str:
    """Encode a task ID and created_at into an opaque base64 cursor."""
    payload = {"id": task_id, "created_at": created_at.isoformat()}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def decode_task_cursor(cursor: str) -> tuple[str, datetime]:
    """Decode an opaque base64 cursor to extract task ID and created_at."""
    try:
        data = json.loads(base64.b64decode(cursor))
        task_id = str(data["id"])
        created_at = datetime.fromisoformat(str(data["created_at"]))
        return task_id, created_at
    except (ValueError, KeyError, TypeError) as e:
        raise InputValidationError(
            code=ErrorCode.INVALID_INPUT,
            message="Invalid pagination cursor.",
            suggestion="Use the cursor value returned from a previous list response.",
        ) from e
