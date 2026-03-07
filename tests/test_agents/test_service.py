"""Tests for AgentService and DatabaseAgentLookup."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from fleet_api.agents.models import AgentStatus
from fleet_api.agents.service import AgentService, DatabaseAgentLookup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    raw_pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_key, base64.b64encode(raw_pub).decode()


def _fake_agent_obj(
    agent_id: str = "test-agent",
    public_key_b64: str = "",
    status: AgentStatus = AgentStatus.REGISTERED,
    last_heartbeat: datetime | None = None,
) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id
    agent.display_name = None
    agent.public_key = public_key_b64
    agent.capabilities = None
    agent.status = status
    agent.registered_at = datetime(2026, 1, 1, tzinfo=UTC)
    agent.last_heartbeat = last_heartbeat
    agent.metadata_ = None
    return agent


# ---------------------------------------------------------------------------
# AgentService tests
# ---------------------------------------------------------------------------


class TestAgentService:
    @pytest.mark.asyncio
    async def test_get_agent_found(self) -> None:
        """get_agent returns the agent when found."""
        session = AsyncMock()
        agent = _fake_agent_obj()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        session.execute = AsyncMock(return_value=mock_result)

        svc = AgentService(session)
        result = await svc.get_agent("test-agent")
        assert result is agent

    @pytest.mark.asyncio
    async def test_get_agent_not_found(self) -> None:
        """get_agent returns None when agent not found."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        svc = AgentService(session)
        result = await svc.get_agent("ghost")
        assert result is None

    @pytest.mark.asyncio
    async def test_register_agent_creates_record(self) -> None:
        """register_agent calls session.add and commit."""
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        svc = AgentService(session)
        await svc.register_agent(
            agent_id="new-agent",
            public_key="AAAA",
            display_name="New Agent",
            capabilities=["execute"],
        )

        session.add.assert_called_once()
        session.commit.assert_awaited_once()
        session.refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_heartbeat_returns_none_if_not_found(self) -> None:
        """heartbeat returns None when agent not found."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        svc = AgentService(session)
        result = await svc.heartbeat("ghost")
        assert result is None

    @pytest.mark.asyncio
    async def test_heartbeat_activates_registered_agent(self) -> None:
        """heartbeat transitions REGISTERED to ACTIVE on first call."""
        session = AsyncMock()
        agent = _fake_agent_obj(status=AgentStatus.REGISTERED)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        svc = AgentService(session)
        result = await svc.heartbeat("test-agent")

        assert result is not None
        assert agent.status == AgentStatus.ACTIVE
        assert agent.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_heartbeat_preserves_active_status(self) -> None:
        """heartbeat does not change status if already ACTIVE."""
        session = AsyncMock()
        agent = _fake_agent_obj(status=AgentStatus.ACTIVE)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        svc = AgentService(session)
        result = await svc.heartbeat("test-agent")

        assert result is not None
        assert agent.status == AgentStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_register_with_endpoint(self) -> None:
        """register_agent stores endpoint in metadata."""
        session = AsyncMock()
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        svc = AgentService(session)
        await svc.register_agent(
            agent_id="ep-agent",
            public_key="AAAA",
            endpoint="https://agent.example.com/callback",
        )

        # Verify the add was called with an agent that has metadata
        added_agent = session.add.call_args[0][0]
        assert added_agent.metadata_ == {"endpoint": "https://agent.example.com/callback"}


# ---------------------------------------------------------------------------
# DatabaseAgentLookup tests
# ---------------------------------------------------------------------------


class TestDatabaseAgentLookup:
    @pytest.mark.asyncio
    async def test_get_public_key_found(self) -> None:
        """Returns Ed25519PublicKey when agent exists."""
        _, pub_b64 = _generate_keypair()

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = pub_b64
        session.execute = AsyncMock(return_value=mock_result)

        lookup = DatabaseAgentLookup(session)
        key = await lookup.get_agent_public_key("test-agent")

        assert key is not None
        # Verify it's a valid Ed25519PublicKey
        raw = key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert len(raw) == 32

    @pytest.mark.asyncio
    async def test_get_public_key_not_found(self) -> None:
        """Returns None when agent does not exist."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        lookup = DatabaseAgentLookup(session)
        key = await lookup.get_agent_public_key("ghost")
        assert key is None

    @pytest.mark.asyncio
    async def test_is_suspended_true(self) -> None:
        """Returns True when agent status is SUSPENDED."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = AgentStatus.SUSPENDED
        session.execute = AsyncMock(return_value=mock_result)

        lookup = DatabaseAgentLookup(session)
        assert await lookup.is_agent_suspended("bad-agent") is True

    @pytest.mark.asyncio
    async def test_is_suspended_false_active(self) -> None:
        """Returns False when agent status is ACTIVE."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = AgentStatus.ACTIVE
        session.execute = AsyncMock(return_value=mock_result)

        lookup = DatabaseAgentLookup(session)
        assert await lookup.is_agent_suspended("good-agent") is False

    @pytest.mark.asyncio
    async def test_is_suspended_false_not_found(self) -> None:
        """Returns False when agent does not exist."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        lookup = DatabaseAgentLookup(session)
        assert await lookup.is_agent_suspended("ghost") is False
