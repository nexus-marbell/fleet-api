"""Background task that marks agents UNREACHABLE after heartbeat timeout.

SAFETY CONSTRAINT (Nexus + Sage explicit agreement):
This monitor marks agent status UNREACHABLE ONLY. It does NOT cancel,
fail, or touch in-flight tasks. Task timeouts (EXECUTION_TIMEOUT / 504)
handle stuck tasks on their own independent timeline. These are separate
mechanisms — do not couple them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fleet_api.agents.models import Agent, AgentStatus

logger = logging.getLogger(__name__)


async def heartbeat_monitor(
    session_factory: async_sessionmaker[AsyncSession],
    timeout_seconds: int = 90,
    sweep_interval: int = 30,
) -> None:
    """Continuously sweep for agents whose heartbeat has expired.

    Runs in an infinite loop, sleeping *sweep_interval* seconds between
    each sweep.  Agents in ACTIVE status whose ``last_heartbeat`` is older
    than *timeout_seconds* are transitioned to UNREACHABLE.

    SAFETY: Only agent status is modified. In-flight tasks (RUNNING,
    ACCEPTED, PAUSED) are deliberately left untouched. Task lifecycle
    has its own independent timeout mechanism.

    Args:
        session_factory: An async SQLAlchemy session maker.
        timeout_seconds: Seconds of heartbeat silence before marking
            an agent UNREACHABLE.
        sweep_interval: Seconds between sweeps.
    """
    while True:
        await asyncio.sleep(sweep_interval)
        try:
            await _sweep(session_factory, timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("heartbeat_monitor: sweep failed, will retry next cycle")


async def _sweep(
    session_factory: async_sessionmaker[AsyncSession],
    timeout_seconds: int,
) -> None:
    """Execute a single sweep: find and mark timed-out agents."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)

    async with session_factory() as session:
        # Find ACTIVE agents whose last heartbeat is older than the cutoff.
        result = await session.execute(
            select(Agent.id, Agent.last_heartbeat)
            .where(Agent.status == AgentStatus.ACTIVE)
            .where(Agent.last_heartbeat < cutoff)
        )
        stale_rows = result.all()

        if not stale_rows:
            return

        stale_ids = [row.id for row in stale_rows]

        # Bulk update in a single statement for efficiency.
        await session.execute(
            update(Agent)
            .where(Agent.id.in_(stale_ids))
            .where(Agent.status == AgentStatus.ACTIVE)
            .values(status=AgentStatus.UNREACHABLE)
        )
        await session.commit()

        for row in stale_rows:
            elapsed = 0.0
            if row.last_heartbeat:
                elapsed = (datetime.now(UTC) - row.last_heartbeat).total_seconds()
            logger.warning(
                "Agent %s marked UNREACHABLE (no heartbeat for %.0fs)",
                row.id,
                elapsed,
            )
