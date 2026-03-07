"""Local health endpoint for the fleet agent sidecar.

Exposes ``GET /fleet/health`` on ``FLEET_SIDECAR_PORT`` (default 8001).
Reports poller status, fleet-api reachability, and active task count.
"""

from __future__ import annotations

import time

import httpx
from fastapi import FastAPI

from fleet_agent.models import HealthStatus
from fleet_agent.poller import TaskPoller

_app = FastAPI(title="Fleet Agent Sidecar Health")

# Module-level state injected by __main__ at startup.
# Cold start returns unhealthy until configure() is called with a valid
# poller.  This is intentional — the sidecar should not report healthy
# before it has verified fleet-api connectivity.
_poller: TaskPoller | None = None
_fleet_api_url: str = ""
_agent_id: str = ""
_start_time: float = 0.0

# Latency threshold for "degraded" status (seconds).
_DEGRADED_LATENCY_THRESHOLD = 5.0


def configure(poller: TaskPoller, fleet_api_url: str, agent_id: str) -> None:
    """Inject runtime references.  Called once from ``__main__``."""
    global _poller, _fleet_api_url, _agent_id, _start_time  # noqa: PLW0603
    _poller = poller
    _fleet_api_url = fleet_api_url
    _agent_id = agent_id
    _start_time = time.monotonic()


def get_app() -> FastAPI:
    """Return the FastAPI app for this module."""
    return _app


@_app.get("/fleet/health")
async def health() -> HealthStatus:
    """Return sidecar health status."""
    fleet_reachable, latency_ms = await _check_fleet_api()
    poller_running = _poller.is_running if _poller else False
    active_tasks = _poller.active_task_count if _poller else 0
    uptime = time.monotonic() - _start_time if _start_time else 0.0

    if not poller_running or not fleet_reachable:
        status = "unhealthy"
    elif latency_ms is not None and latency_ms > _DEGRADED_LATENCY_THRESHOLD * 1000:
        status = "degraded"
    else:
        status = "healthy"

    return HealthStatus(
        status=status,
        agent_id=_agent_id,
        fleet_api_url=_fleet_api_url,
        fleet_api_reachable=fleet_reachable,
        poller_running=poller_running,
        active_tasks=active_tasks,
        uptime_seconds=int(uptime),
        fleet_api_latency_ms=int(latency_ms) if latency_ms is not None else None,
    )


async def _check_fleet_api() -> tuple[bool, float | None]:
    """Probe the fleet-api health endpoint.

    Returns
    -------
    tuple[bool, float | None]
        A (reachable, latency_ms) pair.  *latency_ms* is ``None`` when the
        endpoint is unreachable.
    """
    if not _fleet_api_url:
        return False, None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            start = time.monotonic()
            response = await client.get(f"{_fleet_api_url.rstrip('/')}/health")
            elapsed_ms = (time.monotonic() - start) * 1000
            return response.status_code == 200, elapsed_ms
    except (httpx.ConnectError, httpx.TimeoutException):
        return False, None
