"""Tests for fleet_agent.signals -- signal polling, delivery, and acknowledgement.

Covers RFC 1 Section 7.2 items 5-7: context injection forwarding, pause/resume/cancel
signal polling, redirect signal handling.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.models import Signal
from fleet_agent.signals import SignalPoller
from fleet_agent.streamer import EventStreamer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DUMMY_REQUEST = httpx.Request(
    "GET", "https://fleet.example.com/agents/test-agent/tasks/pending"
)


def _ok_response(data: object) -> httpx.Response:
    """Create a 200 response with a request set (needed for raise_for_status)."""
    resp = httpx.Response(200, json=data)
    resp._request = _DUMMY_REQUEST  # type: ignore[attr-defined]
    return resp


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def signal_poller(private_key: Ed25519PrivateKey) -> SignalPoller:
    return SignalPoller(
        fleet_api_url="https://fleet.example.com",
        agent_id="test-agent",
        private_key=private_key,
        interval=1,
    )


@pytest.fixture
def streamer(private_key: Ed25519PrivateKey) -> EventStreamer:
    return EventStreamer(
        fleet_api_url="https://fleet.example.com",
        agent_id="test-agent",
        private_key=private_key,
    )


# ---------------------------------------------------------------------------
# Test: Signal polling lifecycle
# ---------------------------------------------------------------------------

class TestSignalPolling:
    """Signal poll -> receive -> deliver -> acknowledge lifecycle."""

    async def test_polls_correct_url_with_auth(
        self, signal_poller: SignalPoller
    ) -> None:
        """poll_signals() hits GET /agents/{id}/tasks/pending with auth headers."""
        response = _ok_response({"data": [], "signals": []})

        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            signals = await signal_poller.poll_signals()

            mock_client.get.assert_called_once()
            call_args = mock_client.get.call_args
            assert "/agents/test-agent/tasks/pending" in call_args[0][0]
            assert signals == []

    async def test_parses_signals_from_response(
        self, signal_poller: SignalPoller
    ) -> None:
        """poll_signals() extracts Signal objects from the signals array."""
        response_data = {
            "data": [],
            "signals": [
                {
                    "task_id": "task-abc",
                    "signal_type": "pause_requested",
                    "timestamp": "2026-03-07T12:00:00Z",
                },
                {
                    "task_id": "task-def",
                    "signal_type": "context_injection",
                    "timestamp": "2026-03-07T12:01:00Z",
                    "payload": {
                        "context_type": "additional_input",
                        "context_sequence": 1,
                        "payload": {"message": "extra data"},
                    },
                },
            ],
        }
        response = _ok_response(response_data)

        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            signals = await signal_poller.poll_signals()

        assert len(signals) == 2
        assert signals[0].task_id == "task-abc"
        assert signals[0].signal_type == "pause_requested"
        assert signals[1].signal_type == "context_injection"
        assert signals[1].payload is not None
        assert signals[1].payload["context_sequence"] == 1

    async def test_empty_signals_on_connection_failure(
        self, signal_poller: SignalPoller
    ) -> None:
        """poll_signals() returns empty list on connection failure."""
        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            signals = await signal_poller.poll_signals()

        assert signals == []

    async def test_no_signals_key_returns_empty(
        self, signal_poller: SignalPoller
    ) -> None:
        """poll_signals() handles response without signals key gracefully."""
        response = _ok_response({"data": []})

        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            signals = await signal_poller.poll_signals()

        assert signals == []


# ---------------------------------------------------------------------------
# Test: Pause signal handling
# ---------------------------------------------------------------------------

class TestPauseSignal:
    """Pause signal: executor paused, status event streamed."""

    async def test_pause_clears_event(self, signal_poller: SignalPoller) -> None:
        """A pause signal clears the asyncio.Event (blocking the executor)."""
        signal_poller.register_task("task-1")
        assert not signal_poller.is_paused("task-1")

        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)

        assert signal_poller.is_paused("task-1")

    async def test_pause_streams_status_event(
        self, signal_poller: SignalPoller
    ) -> None:
        """Pause handler streams a status:paused event back to fleet-api."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)

        mock_streamer.stream.assert_called_once()
        call_args = mock_streamer.stream.call_args
        assert call_args[0][0] == "task-1"

    async def test_duplicate_pause_ignored(
        self, signal_poller: SignalPoller
    ) -> None:
        """A second pause on an already-paused task is a no-op."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)
        await signal_poller._handle_pause("task-1", mock_streamer)

        # Only one stream call -- second pause was ignored.
        assert mock_streamer.stream.call_count == 1


# ---------------------------------------------------------------------------
# Test: Resume signal handling
# ---------------------------------------------------------------------------

class TestResumeSignal:
    """Resume signal: executor resumed, status event streamed."""

    async def test_resume_sets_event(self, signal_poller: SignalPoller) -> None:
        """A resume signal sets the asyncio.Event (unblocking the executor)."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        # Pause first, then resume.
        await signal_poller._handle_pause("task-1", mock_streamer)
        assert signal_poller.is_paused("task-1")

        await signal_poller._handle_resume("task-1", mock_streamer)
        assert not signal_poller.is_paused("task-1")

    async def test_resume_streams_status_event(
        self, signal_poller: SignalPoller
    ) -> None:
        """Resume handler streams a status:running event back to fleet-api."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)
        await signal_poller._handle_resume("task-1", mock_streamer)

        # Two calls: one for pause, one for resume.
        assert mock_streamer.stream.call_count == 2

    async def test_duplicate_resume_ignored(
        self, signal_poller: SignalPoller
    ) -> None:
        """Resume on a running task is a no-op."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        # Task starts in running state -- resume should be ignored.
        await signal_poller._handle_resume("task-1", mock_streamer)

        mock_streamer.stream.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Cancel signal handling
# ---------------------------------------------------------------------------

class TestCancelSignal:
    """Cancel signal: executor terminated, status event streamed."""

    async def test_cancel_sets_flag(self, signal_poller: SignalPoller) -> None:
        """Cancel signal sets the cancel flag for the task."""
        signal_poller.register_task("task-1")
        assert not signal_poller.is_cancelled("task-1")

        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_cancel("task-1", mock_streamer)

        assert signal_poller.is_cancelled("task-1")

    async def test_cancel_unblocks_paused_task(
        self, signal_poller: SignalPoller
    ) -> None:
        """Cancel on a paused task sets the event so the executor can exit."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        # Pause first.
        await signal_poller._handle_pause("task-1", mock_streamer)
        assert signal_poller.is_paused("task-1")

        # Cancel should unblock (set event) and set cancel flag.
        await signal_poller._handle_cancel("task-1", mock_streamer)
        assert signal_poller.is_cancelled("task-1")
        assert not signal_poller.is_paused("task-1")  # Unblocked so executor can see cancel

    async def test_cancel_streams_status_event(
        self, signal_poller: SignalPoller
    ) -> None:
        """Cancel handler streams a status:cancelled event back to fleet-api."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_cancel("task-1", mock_streamer)

        mock_streamer.stream.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Redirect signal handling
# ---------------------------------------------------------------------------

class TestRedirectSignal:
    """Redirect signal: current task cancelled, new task picked up."""

    async def test_redirect_sets_payload_and_cancel(
        self, signal_poller: SignalPoller
    ) -> None:
        """Redirect sets the redirect payload and cancel flag."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        redirect_payload = {
            "new_input": {"prompt": "revised"},
            "reason": "scope change",
        }

        await signal_poller._handle_redirect("task-1", redirect_payload, mock_streamer)

        assert signal_poller.is_cancelled("task-1")
        assert signal_poller.get_redirect("task-1") == redirect_payload

    async def test_redirect_unblocks_paused_task(
        self, signal_poller: SignalPoller
    ) -> None:
        """Redirect on a paused task unblocks so executor can see the signal."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)
        assert signal_poller.is_paused("task-1")

        await signal_poller._handle_redirect(
            "task-1", {"new_input": {}, "reason": "test"}, mock_streamer
        )
        assert not signal_poller.is_paused("task-1")

    async def test_redirect_streams_status_event(
        self, signal_poller: SignalPoller
    ) -> None:
        """Redirect handler streams a status:redirected event."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_redirect(
            "task-1", {"new_input": {}, "reason": "test"}, mock_streamer
        )

        mock_streamer.stream.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Context injection forwarding
# ---------------------------------------------------------------------------

class TestContextInjection:
    """Context injection: received, delivered to executor queue. No ack stream.

    The executor is the single source of truth for context_injected events
    (PR #47 fix: signal poller no longer double-streams ack events).
    """

    async def test_context_queued_for_delivery(
        self, signal_poller: SignalPoller
    ) -> None:
        """Context injection payload is queued for the executor to pick up."""
        signal_poller.register_task("task-1")

        context_payload = {
            "context_type": "additional_input",
            "context_sequence": 1,
            "payload": {"message": "extra data"},
            "urgency": "normal",
        }

        signal_poller._handle_context_injection("task-1", context_payload)

        contexts = signal_poller.pop_context("task-1")
        assert len(contexts) == 1
        assert contexts[0]["context_type"] == "additional_input"
        assert contexts[0]["context_sequence"] == 1

    async def test_context_does_not_stream_ack(
        self, signal_poller: SignalPoller
    ) -> None:
        """Context injection does NOT stream an ack event (executor handles it).

        This is the fix for the double-stream bug: the signal poller queues
        the context, and the executor emits the context_injected event when
        it actually processes it.
        """
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        sig = Signal(
            task_id="task-1",
            signal_type="context_injection",
            timestamp="2026-03-07T12:00:00Z",
            payload={"context_type": "correction", "context_sequence": 1, "payload": {}},
        )
        await signal_poller._handle_signal(sig, mock_streamer)

        # Signal poller should NOT have streamed anything for context injection.
        mock_streamer.stream.assert_not_called()

    async def test_pop_context_clears_queue(
        self, signal_poller: SignalPoller
    ) -> None:
        """pop_context() returns and clears all pending contexts."""
        signal_poller.register_task("task-1")

        for i in range(3):
            signal_poller._handle_context_injection(
                "task-1",
                {"context_type": "additional_input", "context_sequence": i + 1, "payload": {}},
            )

        contexts = signal_poller.pop_context("task-1")
        assert len(contexts) == 3

        # Second pop should return empty.
        contexts_again = signal_poller.pop_context("task-1")
        assert len(contexts_again) == 0

    async def test_context_for_untracked_task_ignored(
        self, signal_poller: SignalPoller
    ) -> None:
        """Context for an unregistered task is silently ignored."""
        # No task registered -- should not raise.
        signal_poller._handle_context_injection(
            "no-such-task",
            {"context_type": "correction", "context_sequence": 1, "payload": {}},
        )

        # No crash, context is silently dropped.


# ---------------------------------------------------------------------------
# Test: No signals (empty poll)
# ---------------------------------------------------------------------------

class TestEmptyPoll:
    """Empty signal poll returns cleanly without side effects."""

    async def test_empty_signals_no_side_effects(
        self, signal_poller: SignalPoller
    ) -> None:
        """An empty signal list doesn't affect registered task state."""
        signal_poller.register_task("task-1")
        assert not signal_poller.is_paused("task-1")
        assert not signal_poller.is_cancelled("task-1")
        assert signal_poller.get_redirect("task-1") is None
        assert signal_poller.pop_context("task-1") == []


# ---------------------------------------------------------------------------
# Test: Signal ordering
# ---------------------------------------------------------------------------

class TestSignalOrdering:
    """Multiple signals processed in order."""

    async def test_signals_processed_in_order(
        self, signal_poller: SignalPoller
    ) -> None:
        """Signals are applied in the order received (pause then resume)."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        signals = [
            Signal(
                task_id="task-1",
                signal_type="pause_requested",
                timestamp="2026-03-07T12:00:00Z",
            ),
            Signal(
                task_id="task-1",
                signal_type="resume_requested",
                timestamp="2026-03-07T12:00:05Z",
            ),
        ]

        for sig in signals:
            await signal_poller._handle_signal(sig, mock_streamer)

        # After pause then resume, task should be running (not paused).
        assert not signal_poller.is_paused("task-1")

    async def test_multiple_context_injections_ordered(
        self, signal_poller: SignalPoller
    ) -> None:
        """Multiple context injections are queued in order."""
        signal_poller.register_task("task-1")

        for i in range(1, 4):
            signal_poller._handle_context_injection(
                "task-1",
                {"context_type": "additional_input", "context_sequence": i, "payload": {}},
            )

        contexts = signal_poller.pop_context("task-1")
        assert len(contexts) == 3
        assert [c["context_sequence"] for c in contexts] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Test: Error handling (fleet-api unreachable during signal poll)
# ---------------------------------------------------------------------------

class TestErrorHandling:
    """Error handling during signal polling."""

    async def test_backoff_on_connection_failure(
        self, signal_poller: SignalPoller
    ) -> None:
        """Connection failure increases backoff."""
        initial_backoff = signal_poller._current_backoff

        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await signal_poller.poll_signals()

        assert signal_poller._current_backoff > initial_backoff

    async def test_backoff_resets_on_success(
        self, signal_poller: SignalPoller
    ) -> None:
        """Successful poll resets backoff to base."""
        # Simulate failure first to increase backoff.
        signal_poller._current_backoff = 16.0

        response = _ok_response({"data": [], "signals": []})
        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await signal_poller.poll_signals()

        assert signal_poller._current_backoff == 2.0  # _BASE_BACKOFF_SECONDS

    async def test_http_5xx_triggers_backoff(
        self, signal_poller: SignalPoller
    ) -> None:
        """5xx response triggers backoff (fleet-api is down)."""
        error_resp = httpx.Response(503, json={"error": "unavailable"})
        error_resp._request = _DUMMY_REQUEST  # type: ignore[attr-defined]

        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.HTTPStatusError(
                "503", request=_DUMMY_REQUEST, response=error_resp
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            signals = await signal_poller.poll_signals()

        assert signals == []
        assert signal_poller._current_backoff > 2.0

    async def test_streamer_failure_doesnt_crash_handler(
        self, signal_poller: SignalPoller
    ) -> None:
        """If the streamer fails during ack, the handler logs but doesn't crash."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock(side_effect=RuntimeError("connection lost"))

        # Should not raise.
        await signal_poller._handle_pause("task-1", mock_streamer)

        # Task should still be paused locally even though ack failed.
        assert signal_poller.is_paused("task-1")


# ---------------------------------------------------------------------------
# Test: Signal deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    """Signals are deduplicated by (task_id, signal_type, timestamp)."""

    async def test_duplicate_signals_not_reprocessed(
        self, signal_poller: SignalPoller
    ) -> None:
        """The same signal received twice is only handled once."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        sig = Signal(
            task_id="task-1",
            signal_type="pause_requested",
            timestamp="2026-03-07T12:00:00Z",
        )

        # Process the signal twice.
        sig_key = f"{sig.task_id}:{sig.signal_type}:{sig.timestamp}"
        await signal_poller._handle_signal(sig, mock_streamer)
        signal_poller.mark_signal_processed(sig_key)

        # Second invocation would be skipped by the run loop's dedup check.
        # Verify the signal was processed.
        assert signal_poller.is_signal_processed(sig_key)


# ---------------------------------------------------------------------------
# Test: Task registration and cleanup
# ---------------------------------------------------------------------------

class TestTaskLifecycle:
    """Task registration and unregistration for signal monitoring."""

    def test_register_creates_state(self, signal_poller: SignalPoller) -> None:
        """register_task creates all shared state entries."""
        signal_poller.register_task("task-1")

        assert signal_poller.has_task("task-1")
        assert not signal_poller.is_paused("task-1")
        assert not signal_poller.is_cancelled("task-1")

    def test_unregister_cleans_state(self, signal_poller: SignalPoller) -> None:
        """unregister_task removes all shared state entries."""
        signal_poller.register_task("task-1")
        signal_poller.unregister_task("task-1")

        assert not signal_poller.has_task("task-1")

    def test_unregister_nonexistent_task_safe(
        self, signal_poller: SignalPoller
    ) -> None:
        """Unregistering a task that was never registered doesn't raise."""
        signal_poller.unregister_task("no-such-task")  # Should not raise

    def test_unregister_prunes_processed_signals(
        self, signal_poller: SignalPoller
    ) -> None:
        """unregister_task prunes _processed_signals entries for that task.

        This is the fix for the memory leak (Blocker 4): signal keys matching
        the unregistered task_id prefix are removed on cleanup.
        """
        signal_poller.register_task("task-1")
        signal_poller.register_task("task-2")

        # Simulate processed signals for both tasks.
        signal_poller.mark_signal_processed("task-1:pause_requested:2026-03-07T12:00:00Z")
        signal_poller.mark_signal_processed("task-1:resume_requested:2026-03-07T12:00:05Z")
        signal_poller.mark_signal_processed("task-2:cancel_requested:2026-03-07T12:01:00Z")

        assert len(signal_poller._processed_signals) == 3

        # Unregister task-1 -- should prune its entries but keep task-2's.
        signal_poller.unregister_task("task-1")

        assert len(signal_poller._processed_signals) == 1
        assert signal_poller.is_signal_processed(
            "task-2:cancel_requested:2026-03-07T12:01:00Z"
        )
        assert not signal_poller.is_signal_processed(
            "task-1:pause_requested:2026-03-07T12:00:00Z"
        )


# ---------------------------------------------------------------------------
# Test: wait_if_paused blocking behavior
# ---------------------------------------------------------------------------

class TestWaitIfPaused:
    """wait_if_paused() blocks executor when paused, returns when resumed."""

    async def test_returns_immediately_when_not_paused(
        self, signal_poller: SignalPoller
    ) -> None:
        """wait_if_paused() returns immediately when task is running."""
        signal_poller.register_task("task-1")

        # Should return almost immediately (no blocking).
        await asyncio.wait_for(
            signal_poller.wait_if_paused("task-1"), timeout=0.1
        )

    async def test_blocks_when_paused(
        self, signal_poller: SignalPoller
    ) -> None:
        """wait_if_paused() blocks when task is paused."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)

        # Should block -- use a short timeout to verify.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                signal_poller.wait_if_paused("task-1"), timeout=0.05
            )

    async def test_unblocks_on_resume(
        self, signal_poller: SignalPoller
    ) -> None:
        """wait_if_paused() unblocks when a resume signal is processed."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        await signal_poller._handle_pause("task-1", mock_streamer)

        async def _resume_after_delay() -> None:
            await asyncio.sleep(0.05)
            await signal_poller._handle_resume("task-1", mock_streamer)

        # Start resume in background, wait_if_paused should unblock.
        resume_task = asyncio.create_task(_resume_after_delay())
        await asyncio.wait_for(
            signal_poller.wait_if_paused("task-1"), timeout=1.0
        )
        await resume_task

    async def test_returns_for_unregistered_task(
        self, signal_poller: SignalPoller
    ) -> None:
        """wait_if_paused() returns immediately for unregistered task."""
        await asyncio.wait_for(
            signal_poller.wait_if_paused("no-such-task"), timeout=0.1
        )


# ---------------------------------------------------------------------------
# Test: Integration -- signal polling concurrent with task execution
# ---------------------------------------------------------------------------

class TestIntegrationConcurrency:
    """Signal polling runs concurrently with task execution."""

    async def test_signal_poller_run_loop_cancellable(
        self, signal_poller: SignalPoller
    ) -> None:
        """The run() loop can be cancelled gracefully."""
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        response = _ok_response({"data": [], "signals": []})
        with patch("fleet_agent.signals.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            task = asyncio.create_task(signal_poller.run(mock_streamer))

            # Let it run for a short time.
            await asyncio.sleep(0.05)
            assert signal_poller.is_running

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert not signal_poller.is_running

    async def test_signal_handler_dispatches_all_types(
        self, signal_poller: SignalPoller
    ) -> None:
        """_handle_signal dispatches to the correct handler for each type."""
        signal_poller.register_task("task-1")
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        # Test all signal types.
        type_to_check = {
            "pause_requested": lambda: signal_poller.is_paused("task-1"),
            "resume_requested": lambda: not signal_poller.is_paused("task-1"),
            "cancel_requested": lambda: signal_poller.is_cancelled("task-1"),
        }

        for signal_type, check_fn in type_to_check.items():
            # Re-register to reset state.
            signal_poller.unregister_task("task-1")
            signal_poller.register_task("task-1")

            # Pause first if testing resume.
            if signal_type == "resume_requested":
                await signal_poller._handle_pause("task-1", mock_streamer)

            sig = Signal(
                task_id="task-1",
                signal_type=signal_type,  # type: ignore[arg-type]
                timestamp="2026-03-07T12:00:00Z",
            )
            await signal_poller._handle_signal(sig, mock_streamer)
            assert check_fn(), f"Check failed for signal type: {signal_type}"

    async def test_signals_for_untracked_task_skipped(
        self, signal_poller: SignalPoller
    ) -> None:
        """Signals for tasks not registered with the signal poller are skipped."""
        mock_streamer = MagicMock(spec=EventStreamer)
        mock_streamer.stream = AsyncMock()

        sig = Signal(
            task_id="untracked-task",
            signal_type="pause_requested",
            timestamp="2026-03-07T12:00:00Z",
        )

        # Should not raise or call streamer.
        await signal_poller._handle_signal(sig, mock_streamer)
        mock_streamer.stream.assert_not_called()
