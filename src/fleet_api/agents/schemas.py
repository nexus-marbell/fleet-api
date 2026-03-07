"""Pydantic request/response models for agent endpoints."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*$")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class RegisterAgentRequest(BaseModel):
    """POST /agents/register request body."""

    agent_id: str
    display_name: str | None = None
    public_key: str  # base64-encoded Ed25519 raw public key (32 bytes)
    capabilities: list[str] | None = None
    endpoint: str | None = None

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("agent_id must not be empty")
        if len(v) > 128:
            raise ValueError("agent_id must be at most 128 characters")
        if not _AGENT_ID_PATTERN.match(v):
            raise ValueError(
                "agent_id must contain only alphanumeric characters and hyphens, "
                "and must start with an alphanumeric character"
            )
        return v

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, v: str) -> str:
        import base64

        if not v or not v.strip():
            raise ValueError("public_key must not be empty")
        try:
            decoded = base64.b64decode(v)
        except Exception:
            raise ValueError("public_key must be valid base64")
        if len(decoded) != 32:
            raise ValueError(
                f"public_key must decode to exactly 32 bytes (Ed25519), got {len(decoded)}"
            )
        return v


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class LinkObject(BaseModel):
    """HATEOAS link."""

    href: str
    method: str | None = None

    model_config = ConfigDict(extra="forbid")


class OnboardingStep(BaseModel):
    """Pattern 13: onboarding step."""

    step: int
    action: str
    endpoint: str | None = None
    hint: str | None = None


class AgentResponse(BaseModel):
    """Full agent profile response."""

    agent_id: str
    display_name: str | None = None
    public_key: str
    capabilities: list[str] | None = None
    status: str
    registered_at: datetime
    last_heartbeat: datetime | None = None
    _links: dict[str, LinkObject] = {}

    model_config = ConfigDict(populate_by_name=True)


class RegisterAgentResponse(BaseModel):
    """POST /agents/register response body."""

    agent_id: str
    display_name: str | None = None
    public_key: str
    capabilities: list[str] | None = None
    status: str
    registered_at: datetime
    onboarding: list[OnboardingStep] = []
    _links: dict[str, LinkObject] = {}

    model_config = ConfigDict(populate_by_name=True)


class HeartbeatResponse(BaseModel):
    """PUT /agents/{agent_id}/heartbeat response body."""

    agent_id: str
    status: str
    last_heartbeat: datetime
    _links: dict[str, LinkObject] = {}

    model_config = ConfigDict(populate_by_name=True)
