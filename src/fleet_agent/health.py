"""Local health endpoint for the fleet agent sidecar.

Exposes ``GET /fleet/health`` on ``FLEET_SIDECAR_PORT`` (default 8001).
Reports poller status, fleet-api reachability, and active task count.
"""

from __future__ import annotations

import time

import httpx
from fastapi import FastAPI

from fleet_agent.poller import TaskPoller

_app = FastAPI(title="Fleet Agent Sidecar Health")

# Module-level state injected by __main__ at startup.
_poller: TaskPoller | None = None
_fleet_api_url: str = ""
_agent_id: str = ""
_start_time: float = 0.0


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
async def health() -> dict:  # type: ignore[type-arg]
    """Return sidecar health status."""
    fleet_reachable = await _check_fleet_api()
    poller_running = _poller.is_running if _poller else False
    active_tasks = _poller.active_task_count if _poller else 0
    uptime = time.monotonic() - _start_time if _start_time else 0.0

    status = "healthy" if (poller_running and fleet_reachable) else "unhealthy"

    return {
        "status": status,
        "agent_id": _agent_id,
        "fleet_api_url": _fleet_api_url,
        "fleet_api_reachable": fleet_reachable,
        "poller_running": poller_running,
        "active_tasks": active_tasks,
        "uptime_seconds": int(uptime),
    }


async def _check_fleet_api() -> bool:
    """Probe the fleet-api health endpoint."""
    if not _fleet_api_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{_fleet_api_url.rstrip('/')}/health")
            return response.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False
