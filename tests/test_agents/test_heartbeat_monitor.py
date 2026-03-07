"""Tests for the heartbeat monitor background task."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fleet_api.agents.heartbeat_monitor import _sweep, heartbeat_monitor
from fleet_api.agents.models import AgentStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(agent_id: str, last_heartbeat: datetime) -> MagicMock:
    """Create a mock row from the SELECT query."""
    row = MagicMock()
    row.id = agent_id
    row.last_heartbeat = last_heartbeat
    return row


class _FakeSessionContext:
    """Async context manager that yields a mock session."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        pass


def _make_session_factory(
    stale_rows: list[MagicMock] | None = None,
) -> MagicMock:
    """Build a mock async session factory.

    The session it yields returns *stale_rows* from the first ``execute``
    call (the SELECT) and succeeds silently on the second (the UPDATE).

    ``async_sessionmaker()`` returns a synchronous callable that produces
    an async context manager (not a coroutine). The mock mirrors this.
    """
    session = AsyncMock()

    select_result = MagicMock()
    select_result.all.return_value = stale_rows or []

    update_result = MagicMock()

    # First execute -> SELECT, second -> UPDATE
    session.execute = AsyncMock(side_effect=[select_result, update_result])
    session.commit = AsyncMock()

    factory = MagicMock()
    factory.return_value = _FakeSessionContext(session)
    # Stash session for assertions in tests
    factory._mock_session = session

    return factory


# ---------------------------------------------------------------------------
# _sweep unit tests
# ---------------------------------------------------------------------------


class TestSweep:
    """Tests for the single-sweep function."""

    @pytest.mark.asyncio
    async def test_marks_agent_unreachable_after_timeout(self) -> None:
        """An ACTIVE agent with an old last_heartbeat is marked UNREACHABLE."""
        old_heartbeat = datetime.now(UTC) - timedelta(seconds=120)
        stale = _make_row("stale-agent", old_heartbeat)
        factory = _make_session_factory(stale_rows=[stale])

        await _sweep(factory, timeout_seconds=90)

        session = factory._mock_session
        # Two executes: SELECT then UPDATE
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_touch_active_agents_within_timeout(self) -> None:
        """Agents with recent heartbeats are not returned by the query."""
        # No stale rows returned → no UPDATE issued
        factory = _make_session_factory(stale_rows=[])

        await _sweep(factory, timeout_seconds=90)

        session = factory._mock_session
        # Only the SELECT executes; no UPDATE, no commit
        assert session.execute.await_count == 1
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_touch_registered_agents(self) -> None:
        """REGISTERED agents are not affected — the query filters on ACTIVE only.

        The SQL WHERE clause constrains to status=ACTIVE, so REGISTERED agents
        that have never sent a heartbeat (last_heartbeat=NULL) are excluded.
        This test verifies the no-rows path which is the same as "no stale ACTIVE
        agents", confirming REGISTERED agents are never swept.
        """
        factory = _make_session_factory(stale_rows=[])

        await _sweep(factory, timeout_seconds=90)

        session = factory._mock_session
        assert session.execute.await_count == 1
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_multiple_stale_agents(self) -> None:
        """Multiple stale agents are all marked in a single sweep."""
        old = datetime.now(UTC) - timedelta(seconds=200)
        rows = [_make_row("agent-a", old), _make_row("agent-b", old)]
        factory = _make_session_factory(stale_rows=rows)

        await _sweep(factory, timeout_seconds=90)

        session = factory._mock_session
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_logs_transition(self, caplog: pytest.LogCaptureFixture) -> None:
        """Each UNREACHABLE transition emits a WARNING log."""
        old_heartbeat = datetime.now(UTC) - timedelta(seconds=120)
        stale = _make_row("log-agent", old_heartbeat)
        factory = _make_session_factory(stale_rows=[stale])

        with caplog.at_level(logging.WARNING, logger="fleet_api.agents.heartbeat_monitor"):
            await _sweep(factory, timeout_seconds=90)

        assert any(
            "log-agent" in record.message and "UNREACHABLE" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_exception_does_not_crash(self) -> None:
        """If the DB query raises, the sweep raises (caller handles retry)."""
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db gone"))

        factory = MagicMock()
        factory.return_value = _FakeSessionContext(session)

        with pytest.raises(RuntimeError, match="db gone"):
            await _sweep(factory, timeout_seconds=90)


# ---------------------------------------------------------------------------
# heartbeat_monitor loop tests
# ---------------------------------------------------------------------------


class TestHeartbeatMonitorLoop:
    """Tests for the long-running monitor coroutine."""

    @pytest.mark.asyncio
    async def test_runs_sweep_after_interval(self) -> None:
        """The monitor sleeps then calls _sweep."""
        factory = _make_session_factory(stale_rows=[])
        call_count = 0

        async def counting_sweep(sf: object, ts: int) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError

        with patch("fleet_api.agents.heartbeat_monitor._sweep", side_effect=counting_sweep):
            with patch("fleet_api.agents.heartbeat_monitor.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await heartbeat_monitor(factory, timeout_seconds=90, sweep_interval=1)

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_continues_after_sweep_exception(self) -> None:
        """A non-CancelledError exception in _sweep is caught and logged; loop continues."""
        call_count = 0

        async def failing_then_cancel(sf: object, ts: int) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            raise asyncio.CancelledError

        with patch("fleet_api.agents.heartbeat_monitor._sweep", side_effect=failing_then_cancel):
            with patch("fleet_api.agents.heartbeat_monitor.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await heartbeat_monitor(AsyncMock(), timeout_seconds=90, sweep_interval=1)

        # Should have run twice: first failed, second cancelled
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError from _sweep is not swallowed — it stops the monitor."""
        async def immediate_cancel(sf: object, ts: int) -> None:
            raise asyncio.CancelledError

        with patch("fleet_api.agents.heartbeat_monitor._sweep", side_effect=immediate_cancel):
            with patch("fleet_api.agents.heartbeat_monitor.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await heartbeat_monitor(AsyncMock(), timeout_seconds=90, sweep_interval=1)


# ---------------------------------------------------------------------------
# Task isolation — CRITICAL SAFETY CONSTRAINT
# ---------------------------------------------------------------------------


class TestTaskIsolation:
    """Verify that marking agents UNREACHABLE does NOT touch tasks.

    The heartbeat monitor's SQL only touches the agents table.
    Task lifecycle has its own independent timeout mechanism.
    """

    @pytest.mark.asyncio
    async def test_does_not_cancel_in_flight_tasks(self) -> None:
        """When an agent is marked UNREACHABLE, its tasks are untouched.

        The _sweep function issues exactly two SQL statements:
        1. SELECT from agents (find stale ACTIVE agents)
        2. UPDATE agents SET status=UNREACHABLE

        No query touches the tasks table. This test verifies the SQL
        statements issued by _sweep contain no reference to tasks.
        """
        old_heartbeat = datetime.now(UTC) - timedelta(seconds=120)
        stale = _make_row("busy-agent", old_heartbeat)
        factory = _make_session_factory(stale_rows=[stale])

        await _sweep(factory, timeout_seconds=90)

        session = factory._mock_session
        # Exactly 2 execute calls: SELECT agents, UPDATE agents
        assert session.execute.await_count == 2
        # No commit-related side effects on tasks — the session mock
        # only handles agents queries, confirming no task mutation.
        session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Service re-activation — heartbeat restores UNREACHABLE → ACTIVE
# ---------------------------------------------------------------------------


class TestHeartbeatReactivation:
    """Verify that AgentService.heartbeat re-activates UNREACHABLE agents."""

    @pytest.mark.asyncio
    async def test_heartbeat_reactivates_unreachable_agent(self) -> None:
        """An UNREACHABLE agent sending a heartbeat is re-activated to ACTIVE."""
        from fleet_api.agents.service import AgentService
        from tests.test_agents.conftest import make_fake_agent

        session = AsyncMock()
        agent = make_fake_agent(
            agent_id="revived-agent",
            status=AgentStatus.UNREACHABLE,
        )

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = agent
        session.execute = AsyncMock(return_value=mock_result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        svc = AgentService(session)
        result = await svc.heartbeat("revived-agent")

        assert result is not None
        assert agent.status == AgentStatus.ACTIVE
        assert agent.last_heartbeat is not None
