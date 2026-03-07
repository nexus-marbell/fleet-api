"""Agent business logic and database lookup implementation."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fleet_api.agents.models import Agent, AgentStatus

# ---------------------------------------------------------------------------
# AgentService — CRUD operations for agent endpoints
# ---------------------------------------------------------------------------


class AgentService:
    """Encapsulates agent CRUD operations.

    Accepts an async SQLAlchemy session and provides methods consumed by
    the route handlers.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch an agent by primary key."""
        result = await self._session.execute(
            select(Agent).where(Agent.id == agent_id)
        )
        return result.scalar_one_or_none()

    async def register_agent(
        self,
        agent_id: str,
        public_key: str,
        display_name: str | None = None,
        capabilities: list[str] | None = None,
        endpoint: str | None = None,
    ) -> Agent:
        """Create a new agent record.

        The caller is responsible for checking idempotency / conflict
        conditions before calling this method.
        """
        metadata = {"endpoint": endpoint} if endpoint else None
        agent = Agent(
            id=agent_id,
            display_name=display_name,
            public_key=public_key,
            capabilities=capabilities,
            status=AgentStatus.REGISTERED,
            registered_at=datetime.now(UTC),
            metadata_=metadata,
        )
        self._session.add(agent)
        await self._session.commit()
        await self._session.refresh(agent)
        return agent

    async def heartbeat(self, agent_id: str) -> Agent | None:
        """Record a heartbeat for an agent.

        Transitions from REGISTERED to ACTIVE on first heartbeat.
        Returns the updated agent, or None if not found.
        """
        agent = await self.get_agent(agent_id)
        if agent is None:
            return None

        now = datetime.now(UTC)
        agent.last_heartbeat = now

        # First heartbeat transitions registered -> active;
        # subsequent heartbeat re-activates unreachable agents.
        if agent.status in (AgentStatus.REGISTERED, AgentStatus.UNREACHABLE):
            agent.status = AgentStatus.ACTIVE

        await self._session.commit()
        await self._session.refresh(agent)
        return agent


# ---------------------------------------------------------------------------
# DatabaseAgentLookup — implements the AgentLookup protocol for auth
# ---------------------------------------------------------------------------


class DatabaseAgentLookup:
    """Real AgentLookup implementation backed by the database.

    Satisfies the :class:`~fleet_api.middleware.auth.AgentLookup` protocol
    so that :func:`~fleet_api.middleware.auth.require_auth` can verify
    Ed25519 signatures against registered public keys.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_agent_public_key(self, agent_id: str) -> Ed25519PublicKey | None:
        """Query the database for the agent's Ed25519 public key."""
        result = await self._session.execute(
            select(Agent.public_key).where(Agent.id == agent_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None

        # Decode the base64-stored raw public key into an Ed25519PublicKey
        raw_bytes = base64.b64decode(row)
        return Ed25519PublicKey.from_public_bytes(raw_bytes)

    async def is_agent_suspended(self, agent_id: str) -> bool:
        """Check if the agent is in SUSPENDED status."""
        result = await self._session.execute(
            select(Agent.status).where(Agent.id == agent_id)
        )
        status = result.scalar_one_or_none()
        if status is None:
            return False
        return status == AgentStatus.SUSPENDED
