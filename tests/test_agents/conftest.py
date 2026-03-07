"""Shared test fixtures for agent endpoint tests."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from fleet_api.agents.models import AgentStatus


def generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Generate an Ed25519 keypair, return (private_key, base64_raw_public)."""
    private_key = Ed25519PrivateKey.generate()
    raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_key, base64.b64encode(raw_pub).decode()


def make_fake_agent(
    agent_id: str = "test-agent",
    public_key_b64: str = "",
    display_name: str | None = None,
    capabilities: list[str] | None = None,
    status: AgentStatus = AgentStatus.REGISTERED,
    last_heartbeat: datetime | None = None,
) -> MagicMock:
    """Create a mock Agent ORM object for tests."""
    agent = MagicMock()
    agent.id = agent_id
    agent.display_name = display_name
    agent.public_key = public_key_b64
    agent.capabilities = capabilities
    agent.status = status
    agent.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
    agent.last_heartbeat = last_heartbeat
    agent.metadata_ = None
    return agent


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, str]:
    """Generate a fresh Ed25519 keypair: (private_key, base64_raw_public)."""
    return generate_keypair()
