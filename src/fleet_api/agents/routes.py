"""Agent API routes — registration, heartbeat, profile lookup."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.agents.schemas import (
    AgentResponse,
    HeartbeatResponse,
    LinkObject,
    OnboardingStep,
    RegisterAgentRequest,
    RegisterAgentResponse,
)
from fleet_api.agents.service import AgentService
from fleet_api.database.connection import get_session
from fleet_api.errors import AuthError, ConflictError, ErrorCode, NotFoundError
from fleet_api.middleware.auth import AuthenticatedAgent, require_auth

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_links(agent_id: str) -> dict[str, Any]:
    """HATEOAS links for an agent resource."""
    return {
        "self": LinkObject(href=f"/agents/{agent_id}", method="GET").model_dump(
            exclude_none=True
        ),
        "heartbeat": LinkObject(
            href=f"/agents/{agent_id}/heartbeat", method="POST"
        ).model_dump(exclude_none=True),
        "workflows": LinkObject(href="/workflows", method="GET").model_dump(
            exclude_none=True
        ),
    }


def _onboarding_steps(agent_id: str) -> list[OnboardingStep]:
    """Pattern 13: onboarding steps after registration."""
    return [
        OnboardingStep(
            step=1,
            action="Send your first heartbeat to activate the agent",
            endpoint=f"/agents/{agent_id}/heartbeat",
            hint="POST with a signed request to transition from registered to active",
        ),
        OnboardingStep(
            step=2,
            action="Register a workflow",
            endpoint="/workflows",
            hint="POST a workflow definition to begin accepting tasks",
        ),
        OnboardingStep(
            step=3,
            action="Check the manifest for available endpoints",
            endpoint="/manifest",
            hint="GET /manifest returns the full API surface",
        ),
    ]


def _build_register_response(agent: Any) -> dict[str, Any]:
    """Build the registration response dict."""
    resp = RegisterAgentResponse(
        agent_id=agent.id,
        display_name=agent.display_name,
        public_key=agent.public_key,
        capabilities=agent.capabilities,
        status=agent.status.value if hasattr(agent.status, "value") else str(agent.status),
        registered_at=agent.registered_at,
        onboarding=_onboarding_steps(agent.id),
    )
    # _links injected post-model_dump (Pydantic v2 excludes _-prefixed attrs)
    result = resp.model_dump(mode="json")
    result["_links"] = _agent_links(agent.id)
    return result


# ---------------------------------------------------------------------------
# POST /agents/register (UNAUTHENTICATED — bootstrap)
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    status_code=201,
    response_model=None,
)
async def register_agent(
    body: RegisterAgentRequest,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Register a new agent with an Ed25519 public key.

    Idempotent: re-registering with the same agent_id + public_key returns
    200 with the existing record.  Different public_key for the same
    agent_id returns 409 AGENT_EXISTS.
    """
    svc = AgentService(session)
    existing = await svc.get_agent(body.agent_id)

    if existing is not None:
        if existing.public_key == body.public_key:
            # Idempotent re-registration — return 200 with existing record
            return JSONResponse(
                status_code=200,
                content=_build_register_response(existing),
            )
        # Conflict — different public key
        raise ConflictError(
            code=ErrorCode.AGENT_EXISTS,
            message=(
                f"Agent '{body.agent_id}' is already registered with a different public key. "
                "Re-registration requires the same public key."
            ),
            suggestion="Use the original public key, or register with a different agent_id.",
            links={"existing_agent": {"href": f"/agents/{body.agent_id}"}},
        )

    agent = await svc.register_agent(
        agent_id=body.agent_id,
        public_key=body.public_key,
        display_name=body.display_name,
        capabilities=body.capabilities,
        endpoint=body.endpoint,
    )

    return _build_register_response(agent)


# ---------------------------------------------------------------------------
# POST /agents/{agent_id}/heartbeat (AUTHENTICATED) — RFC §4.3
# ---------------------------------------------------------------------------


@router.post("/{agent_id}/heartbeat")
async def heartbeat(
    agent_id: str,
    auth: AuthenticatedAgent | None = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Record a heartbeat for the authenticated agent.

    The agent in the auth header MUST match the path {agent_id}.
    On first heartbeat, transitions status from registered to active.
    """
    # Auth guard: agent can only heartbeat for itself
    if auth is None or auth.agent_id != agent_id:
        raise AuthError(
            code=ErrorCode.NOT_AUTHORIZED,
            message=(
                f"Agent '{auth.agent_id if auth else 'unknown'}' "
                f"cannot send heartbeat for '{agent_id}'"
            ),
            suggestion="You can only send heartbeats for your own agent_id.",
        )

    svc = AgentService(session)
    agent = await svc.heartbeat(agent_id)

    if agent is None:
        raise NotFoundError(
            code=ErrorCode.ENDPOINT_NOT_FOUND,
            message=f"Agent '{agent_id}' not found",
            suggestion="Register the agent first via POST /agents/register",
            links={"register": {"href": "/agents/register"}},
        )

    resp = HeartbeatResponse(
        agent_id=agent.id,
        status=agent.status.value if hasattr(agent.status, "value") else str(agent.status),
        last_heartbeat=agent.last_heartbeat,
    )
    # _links injected post-model_dump (Pydantic v2 excludes _-prefixed attrs)
    result = resp.model_dump(mode="json")
    result["_links"] = _agent_links(agent.id)
    return result


# ---------------------------------------------------------------------------
# GET /agents/{agent_id} (AUTHENTICATED)
# ---------------------------------------------------------------------------


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    auth: AuthenticatedAgent | None = Depends(require_auth),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return the public profile of an agent.

    Any authenticated agent can view another agent's profile.
    """
    if auth is None:
        raise AuthError(
            code=ErrorCode.INVALID_SIGNATURE,
            message="Authentication required",
        )

    svc = AgentService(session)
    agent = await svc.get_agent(agent_id)

    if agent is None:
        raise NotFoundError(
            code=ErrorCode.ENDPOINT_NOT_FOUND,
            message=f"Agent '{agent_id}' not found",
            suggestion="Check the agent_id or register a new agent via POST /agents/register",
            links={"register": {"href": "/agents/register"}},
        )

    resp = AgentResponse(
        agent_id=agent.id,
        display_name=agent.display_name,
        public_key=agent.public_key,
        capabilities=agent.capabilities,
        status=agent.status.value if hasattr(agent.status, "value") else str(agent.status),
        registered_at=agent.registered_at,
        last_heartbeat=agent.last_heartbeat,
    )
    # _links injected post-model_dump (Pydantic v2 excludes _-prefixed attrs)
    result = resp.model_dump(mode="json")
    result["_links"] = _agent_links(agent.id)
    return result
