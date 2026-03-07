"""SSE (Server-Sent Events) streaming for task lifecycle events.

Provides a streaming endpoint that emits task events in SSE format.
Supports reconnection via the Last-Event-Id header and sends periodic
heartbeat keepalives to maintain the connection.

The event store is the task_events database table (no in-memory state).
Event sequence numbers are monotonic per task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.config import settings
from fleet_api.database.connection import get_session
from fleet_api.errors import ErrorCode, NotFoundError
from fleet_api.middleware.auth import AuthenticatedAgent, require_auth
from fleet_api.tasks.models import Task, TaskEvent, TaskStatus
from fleet_api.tasks.state_machine import TERMINAL_STATES

logger = logging.getLogger(__name__)

# Terminal states that signal the stream should close.
_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(
    s.value for s in TERMINAL_STATES
)

# Polling interval in seconds for new events.
_POLL_INTERVAL: float = 1.0


def format_sse_event(event_type: str, data: dict[str, Any], sequence: int) -> str:
    """Format a single SSE event string.

    Returns a string in the standard SSE wire format::

        id: {sequence}
        event: {event_type}
        data: {json}

    The trailing double newline is included per the SSE specification.
    """
    json_data = json.dumps(data, default=str)
    return f"id: {sequence}\nevent: {event_type}\ndata: {json_data}\n\n"


async def _fetch_events_after(
    session: AsyncSession,
    task_id: str,
    after_sequence: int,
) -> list[TaskEvent]:
    """Fetch task events with sequence > after_sequence, ordered by sequence ASC."""
    stmt = (
        select(TaskEvent)
        .where(TaskEvent.task_id == task_id)
        .where(TaskEvent.sequence > after_sequence)
        .order_by(TaskEvent.sequence.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _get_task_status(session: AsyncSession, task_id: str) -> TaskStatus:
    """Fetch the current status of a task."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found.",
            suggestion="Check the task ID.",
        )
    return task.status if isinstance(task.status, TaskStatus) else TaskStatus(task.status)


async def _event_stream(
    task_id: str,
    last_event_id: int,
    session: AsyncSession,
    heartbeat_interval: int,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted event strings.

    1. Replays all events after last_event_id.
    2. Polls for new events every _POLL_INTERVAL seconds.
    3. Sends heartbeat keepalive every heartbeat_interval seconds if idle.
    4. Stops when a terminal event is detected or the task reaches a terminal state.
    """
    last_sent_sequence = last_event_id
    seconds_since_last_event: float = 0.0

    # Phase 1: Replay existing events
    events = await _fetch_events_after(session, task_id, last_sent_sequence)
    for event in events:
        yield format_sse_event(
            event_type=event.event_type,
            data=event.data if event.data is not None else {},
            sequence=event.sequence,
        )
        last_sent_sequence = event.sequence

        # Check if this event signals a terminal state
        if event.event_type in ("completed", "failed"):
            return
        if event.event_type == "status":
            status_value = (event.data or {}).get("status", "")
            if status_value in _TERMINAL_STATUS_VALUES:
                return

    # Check current task status — it may already be terminal
    current_status = await _get_task_status(session, task_id)
    if current_status in TERMINAL_STATES:
        return

    # Phase 2: Poll for new events
    while True:
        await asyncio.sleep(_POLL_INTERVAL)
        seconds_since_last_event += _POLL_INTERVAL

        # Expire the session cache so we see fresh data
        session.expire_all()

        events = await _fetch_events_after(session, task_id, last_sent_sequence)

        if events:
            seconds_since_last_event = 0.0
            for event in events:
                yield format_sse_event(
                    event_type=event.event_type,
                    data=event.data if event.data is not None else {},
                    sequence=event.sequence,
                )
                last_sent_sequence = event.sequence

                # Check if this event signals a terminal state
                if event.event_type in ("completed", "failed"):
                    return
                if event.event_type == "status":
                    status_value = (event.data or {}).get("status", "")
                    if status_value in _TERMINAL_STATUS_VALUES:
                        return
        else:
            # No new events — check if task reached terminal state via other means
            current_status = await _get_task_status(session, task_id)
            if current_status in TERMINAL_STATES:
                return

        # Send heartbeat keepalive if idle
        if seconds_since_last_event >= heartbeat_interval:
            yield format_sse_event(
                event_type="heartbeat",
                data={"type": "keepalive"},
                sequence=last_sent_sequence,
            )
            seconds_since_last_event = 0.0


async def stream_task_events(
    request: Request,
    workflow_id: str,
    task_id: str,
    agent: AuthenticatedAgent = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
    last_event_id: int | None = Header(None, alias="Last-Event-Id"),
) -> StreamingResponse:
    """Stream task events as Server-Sent Events.

    Supports reconnection via the ``Last-Event-Id`` header. Events after
    that sequence number are replayed, then new events are streamed in
    real time until the task reaches a terminal state.

    Returns ``text/event-stream`` with appropriate caching and proxy headers.
    """
    # Verify workflow exists
    from fleet_api.workflows.models import Workflow

    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFoundError(
            code=ErrorCode.WORKFLOW_NOT_FOUND,
            message=f"Workflow '{workflow_id}' not found.",
            suggestion="Check the workflow ID. Use GET /workflows to list available workflows.",
            links={"list": {"href": "/workflows"}},
        )

    # Verify task exists and belongs to workflow
    task = await session.get(Task, task_id)
    if task is None or task.workflow_id != workflow_id:
        raise NotFoundError(
            code=ErrorCode.TASK_NOT_FOUND,
            message=f"Task '{task_id}' not found in workflow '{workflow_id}'.",
            suggestion="Check the task ID. Use GET /workflows/{workflow_id}/tasks to list tasks.",
            links={"workflow": {"href": f"/workflows/{workflow_id}"}},
        )

    effective_last_id = last_event_id if last_event_id is not None else 0
    heartbeat_interval = settings.fleet_sse_heartbeat_interval

    return StreamingResponse(
        _event_stream(task_id, effective_last_id, session, heartbeat_interval),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
