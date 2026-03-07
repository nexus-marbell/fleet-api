"""GET /manifest -- machine-readable API directory.

Returns the full API manifest including identity, auth configuration,
capabilities, rate limits, parameter conventions, schema changelog,
and HATEOAS links to all top-level endpoints.

Unauthenticated (listed in UNPROTECTED_PATHS).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from fleet_api.config import settings

router = APIRouter()


def _build_manifest() -> dict[str, Any]:
    """Build the manifest payload from current settings."""
    base = settings.base_url.rstrip("/")

    return {
        "name": "Fleet API",
        "version": settings.api_version,
        "description": "Distributed task dispatch for agentic workflows",
        "base_url": base,
        "auth": {
            "type": "ed25519-signature",
            "header": "Authorization",
            "format": "Signature <agent_id>:<base64_signature>",
            "key_registration": "/agents/register",
            "server_public_key": None,  # Phase 2: callback verification
        },
        "capabilities": [
            "workflow_registry",
            "task_dispatch",
        ],
        "rate_limits": {
            "requests_per_minute": settings.rate_limit_rpm,
            "burst": settings.rate_limit_burst,
        },
        "parameter_conventions": {
            "limit": {
                "description": "Maximum number of results to return",
                "type": "integer",
                "default": 20,
                "max": 100,
                "not": ["count", "max", "n", "page_size", "per_page"],
            },
            "cursor": {
                "description": "Opaque pagination cursor from a previous response",
                "type": "string",
                "not": ["page", "offset", "skip", "page_token"],
            },
            "status": {
                "description": "Filter by lifecycle status",
                "type": "string",
                "not": ["state", "phase", "stage"],
            },
        },
        "schema_changelog": [
            {
                "version": "1.0.0",
                "date": "2026-03-07",
                "changes": ["Initial release -- manifest, agents, workflows, tasks"],
                "breaking": False,
            },
        ],
        "_links": {
            "self": {"href": f"{base}/manifest"},
            "agents": {"href": f"{base}/agents"},
            "agents_register": {"href": f"{base}/agents/register", "method": "POST"},
            "workflows": {"href": f"{base}/workflows"},
            "tasks": {"href": f"{base}/tasks"},
            "health": {"href": f"{base}/health"},
            "tools": {"href": f"{base}/tools"},
            "openapi": {"href": f"{base}/openapi.json"},
            "errors": {"href": f"{base}/errors"},
        },
    }


@router.get("/manifest", tags=["discovery"])
async def get_manifest() -> JSONResponse:
    """Return the machine-readable API manifest."""
    manifest = _build_manifest()

    now = int(time.time())
    headers = {
        "X-Schema-Version": settings.api_version,
        "X-RateLimit-Limit": str(settings.rate_limit_rpm),
        "X-RateLimit-Reset": str(now + 60),
        "Cache-Control": "public, max-age=60",
    }

    return JSONResponse(content=manifest, headers=headers)
