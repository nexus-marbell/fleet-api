"""Health endpoint — per-component status with graduated responses.

Unauthenticated (Docker healthchecks, load balancers, monitoring).
Phase 1: database component only. Future: task_queue, agent_connectivity.

Status graduation:
  - All operational  -> 200 "operational"
  - Any degraded     -> 503 "degraded"
  - Any unhealthy    -> 503 "unhealthy"
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.database.connection import get_session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level start time (set on import — closest to process boot)
# ---------------------------------------------------------------------------

_START_MONOTONIC: float = time.monotonic()

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

health_router = APIRouter(tags=["health"])

# ---------------------------------------------------------------------------
# Version — sourced from package metadata (pyproject.toml) so it
# cannot silently diverge on version bump.
# ---------------------------------------------------------------------------

try:
    _VERSION = _pkg_version("fleet-api")
except PackageNotFoundError:
    _VERSION = "0.0.0-dev"


# ---------------------------------------------------------------------------
# Component checks
# ---------------------------------------------------------------------------


async def _check_database(session: AsyncSession) -> dict[str, Any]:
    """Execute SELECT 1 and measure round-trip latency.

    Enforces a 5-second timeout per RFC requirement.
    Returns a component status dict. Never raises.
    """
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=5.0)
        latency_ms = round((time.monotonic() - t0) * 1000)
        return {
            "status": "operational",
            "latency_ms": latency_ms,
            "last_successful_query": datetime.now(UTC).isoformat(),
        }
    except TimeoutError:
        latency_ms = round((time.monotonic() - t0) * 1000)
        logger.error("Database health check timed out after 5s")
        return {
            "status": "degraded",
            "latency_ms": latency_ms,
            "error": "timeout",
        }
    except Exception as exc:
        logger.error("Database health check failed: %s", exc)
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
            "status_page": "/status",
        },
    }

    http_status = 200 if status == "operational" else 503
    return JSONResponse(content=body, status_code=http_status)
