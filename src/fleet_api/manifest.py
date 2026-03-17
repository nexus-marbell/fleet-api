"""GET /manifest -- machine-readable API directory.

Returns the full API manifest including identity, auth configuration,
capabilities, rate limits, parameter conventions, schema changelog,
and HATEOAS links to all top-level endpoints.

Unauthenticated (listed in UNPROTECTED_PATHS).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from fleet_api.config import settings
from fleet_api.crypto import get_server_public_key_pem

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
            "server_public_key": get_server_public_key_pem(),
        },
        "capabilities": [
            "workflow_registry",
            "task_dispatch",
            "sse_streaming",
            "pause_resume",
            "context_injection",
            "retask_with_lineage",
            "redirect",
            "callback_signing",
            "idempotent_writes",
            "pull_dispatch",
            "agent_heartbeat",
        ],
        "rate_limits": {
            "status": "planned",
            "description": "Rate limiting is not yet enforced by middleware",
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
            {
                "version": "1.1.0",
                "date": "2026-03-17",
                "changes": [
                    "SSE streaming for task events",
                    "Pause/resume task lifecycle",
                    "Context injection",
                    "Retask with lineage tracking",
                    "Redirect tasks between agents",
                    "Ed25519 callback signing",
                    "Idempotent writes via Idempotency-Key",
                    "Pull dispatch for agents",
                    "Agent heartbeat monitoring",
                    "Removed phantom _links (tools, errors)",
                    "Rate limits marked as planned (no enforcement middleware)",
                ],
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
            "openapi": {"href": f"{base}/openapi.json"},
        },
    }


@router.get("/manifest", tags=["discovery"])
async def get_manifest() -> JSONResponse:
    """Return the machine-readable API manifest."""
    manifest = _build_manifest()

    headers = {
        "X-Schema-Version": settings.api_version,
        "Cache-Control": "public, max-age=60",
    }

    return JSONResponse(content=manifest, headers=headers)
