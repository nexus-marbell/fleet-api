"""Health endpoint — per-component status with graduated responses.

Unauthenticated (Docker healthchecks, load balancers, monitoring).
Phase 1: database component only. Future: task_queue, agent_connectivity.

Status graduation:
  - All operational  -> 200 "operational"
  - Any degraded     -> 503 "degraded"
  - Any unhealthy    -> 503 "unhealthy"
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.database.connection import get_session

# ---------------------------------------------------------------------------
# Module-level start time (set on import — closest to process boot)
# ---------------------------------------------------------------------------

_START_MONOTONIC: float = time.monotonic()
_START_WALL: datetime = datetime.now(UTC)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

health_router = APIRouter(tags=["health"])

# ---------------------------------------------------------------------------
# Version — mirrors pyproject.toml; kept as a constant to avoid
# runtime inspection overhead on every healthcheck.
# ---------------------------------------------------------------------------

_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Component checks
# ---------------------------------------------------------------------------


async def _check_database(session: AsyncSession) -> dict[str, Any]:
    """Execute SELECT 1 and measure round-trip latency.

    Returns a component status dict. Never raises.
    """
    try:
        t0 = time.monotonic()
        await session.execute(text("SELECT 1"))
        latency_ms = round((time.monotonic() - t0) * 1000)
        return {
            "status": "operational",
            "latency_ms": latency_ms,
            "last_successful_query": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        return {
            "status": "unhealthy",
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Graduation logic
# ---------------------------------------------------------------------------


def _graduate(components: dict[str, dict[str, Any]]) -> str:
    """Determine aggregate status from component statuses.

    - Any unhealthy -> "unhealthy"
    - Any degraded  -> "degraded"
    - Otherwise     -> "operational"
    """
    statuses = {c["status"] for c in components.values()}
    if "unhealthy" in statuses:
        return "unhealthy"
    if "degraded" in statuses:
        return "degraded"
    return "operational"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@health_router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """GET /health — graduated health status.

    Returns 200 when all components are operational, 503 otherwise.
    """
    components: dict[str, dict[str, Any]] = {
        "database": await _check_database(session),
    }

    status = _graduate(components)
    uptime_seconds = int(time.monotonic() - _START_MONOTONIC)

    body: dict[str, Any] = {
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "uptime_seconds": uptime_seconds,
        "version": _VERSION,
        "components": components,
        "_links": {
            "self": "/health",
            "manifest": "/manifest",
        },
    }

    http_status = 200 if status == "operational" else 503
    return JSONResponse(content=body, status_code=http_status)
