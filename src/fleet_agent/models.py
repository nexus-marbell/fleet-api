"""Shared data models for the fleet agent sidecar."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class PendingTask(BaseModel):
    """A task retrieved from fleet-api awaiting local execution."""

    task_id: str
    workflow_id: str
    input: dict[str, Any]
    priority: str
    timeout_seconds: int | None = None
    created_at: str


class TaskEvent(BaseModel):
    """An event emitted during task execution, streamed back to fleet-api."""

    event_type: str  # status, progress, log, completed, failed, heartbeat
    data: dict[str, Any] | None = None
    sequence: int


# ---------------------------------------------------------------------------
# Signal types — RFC 1 §7.2 items 5-7
# ---------------------------------------------------------------------------
# Signal types are deliberately distinct from event types.  Signals flow
# from fleet-api *to* the sidecar (inbound control plane); events flow from
# the sidecar *to* fleet-api (outbound data plane).  Keeping the namespaces
# separate prevents accidental confusion between "what the principal asked"
# (signal) and "what the executor reported" (event).

SignalType = Literal[
    "pause_requested",
    "resume_requested",
    "cancel_requested",
    "redirect_requested",
    "context_injection",
]


class Signal(BaseModel):
    """A control signal delivered from fleet-api to the sidecar.

    Signals are polled via ``GET /agents/{id}/tasks/pending`` alongside
    pending tasks (Option A — RFC-aligned, see agents/routes.py).

    Attributes:
        task_id: The running task this signal targets.
        signal_type: One of the ``SignalType`` literals.
        timestamp: ISO 8601 timestamp when the signal was created server-side.
        payload: Signal-specific data.  For ``context_injection``, contains
            ``context_type``, ``context_sequence``, ``payload``, and ``urgency``.
            For ``redirect_requested``, contains ``new_input``, ``reason``,
            and optional ``inherit_progress`` / ``priority``.
    """

    task_id: str
    signal_type: SignalType
    timestamp: str
    payload: dict[str, Any] | None = None


class HealthStatus(BaseModel):
    """Health status returned by the sidecar's ``/fleet/health`` endpoint."""

    status: str  # healthy, degraded, unhealthy
    agent_id: str
    fleet_api_url: str
    fleet_api_reachable: bool
    poller_running: bool
    active_tasks: int
    uptime_seconds: int
    fleet_api_latency_ms: int | None = None
