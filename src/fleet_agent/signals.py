"""Signal poller — polls fleet-api for pending signals on in-flight tasks.

RFC 1 §7.2 items 5-7: context injection forwarding, pause/resume/cancel
signal polling, redirect signal handling.

Signals flow from the principal through fleet-api to the sidecar.  The sidecar
picks them up on the next poll of ``GET /agents/{id}/tasks/pending`` (which
returns both pending tasks and pending signals in the ``signals`` array).

This module is responsible for:
1. Polling for signals at ``fleet_signal_poll_interval``
2. Dispatching each signal to the appropriate handler
3. Acknowledging signals back to fleet-api via event streaming

Architecture note — pull model only (RFC 1 §7.1):
    The sidecar never receives pushes.  All communication is outbound HTTPS
    from the sidecar to fleet-api, which keeps agents behind NAT reachable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.models import Signal, TaskEvent
from fleet_agent.signing import sign_request
from fleet_agent.streamer import EventStreamer

logger = logging.getLogger(__name__)

# Backoff configuration for signal poll failures.
_BASE_BACKOFF_SECONDS = 2.0
_MAX_BACKOFF_SECONDS = 30.0


class SignalPoller:
    """Polls fleet-api for pending signals and dispatches them.

    Works alongside the :class:`TaskPoller` — the task poller handles new task
    assignment, while the signal poller handles control signals for in-flight
    tasks (pause, resume, cancel, redirect, context injection).

    Signals are polled from ``GET /agents/{id}/tasks/pending`` which returns
    a ``signals`` array alongside ``data`` (pending tasks).  This is Option A
    from the RFC — extending the existing endpoint rather than adding a new one.
    """

    def __init__(
        self,
        fleet_api_url: str,
        agent_id: str,
        private_key: Ed25519PrivateKey,
        interval: int = 2,
    ) -> None:
        self._fleet_api_url = fleet_api_url.rstrip("/")
        self._agent_id = agent_id
        self._private_key = private_key
        self._interval = interval
        self._current_backoff = _BASE_BACKOFF_SECONDS
        self._running = False

        # Shared state: maps task_id -> asyncio.Event for pause/resume.
        # When a pause signal arrives, the event is cleared (blocking the executor).
        # When a resume signal arrives, the event is set (unblocking the executor).
        self._pause_events: dict[str, asyncio.Event] = {}

        # Shared state: maps task_id -> bool indicating cancellation requested.
        self._cancel_flags: dict[str, bool] = {}

        # Shared state: maps task_id -> redirect payload (new_input, reason, etc.).
        # When set, the executor should terminate and the poller should pick up
        # the new task on the next poll cycle.
        self._redirect_payloads: dict[str, dict[str, Any]] = {}

        # Shared state: maps task_id -> list of pending context payloads.
        self._context_queue: dict[str, list[dict[str, Any]]] = {}

        # Track which signals have been processed to avoid re-processing.
        self._processed_signals: set[str] = set()

    @property
    def is_running(self) -> bool:
        """Whether the signal polling loop is active."""
        return self._running

    def register_task(self, task_id: str) -> None:
        """Register a task for signal monitoring.

        Creates the shared state entries so the executor can check them.
        Must be called before the executor starts processing a task.
        """
        self._pause_events[task_id] = asyncio.Event()
        self._pause_events[task_id].set()  # Start in "running" (not paused) state
        self._cancel_flags[task_id] = False
        self._redirect_payloads.pop(task_id, None)
        self._context_queue[task_id] = []

    def unregister_task(self, task_id: str) -> None:
        """Clean up signal state for a completed/failed task."""
        self._pause_events.pop(task_id, None)
        self._cancel_flags.pop(task_id, None)
        self._redirect_payloads.pop(task_id, None)
        self._context_queue.pop(task_id, None)

    def is_paused(self, task_id: str) -> bool:
        """Check if a task is currently paused."""
        event = self._pause_events.get(task_id)
        return event is not None and not event.is_set()

    def is_cancelled(self, task_id: str) -> bool:
        """Check if cancellation has been requested for a task."""
        return self._cancel_flags.get(task_id, False)

    def get_redirect(self, task_id: str) -> dict[str, Any] | None:
        """Get pending redirect payload for a task, if any."""
        return self._redirect_payloads.get(task_id)

    def pop_context(self, task_id: str) -> list[dict[str, Any]]:
        """Pop all pending context payloads for a task.

        Returns an empty list if no context is pending.
        """
        contexts = self._context_queue.get(task_id, [])
        if contexts:
            self._context_queue[task_id] = []
        return contexts

    async def wait_if_paused(self, task_id: str) -> None:
        """Block until the task is resumed (or return immediately if not paused).

        Called by the executor between processing steps to honor pause signals.
        """
        event = self._pause_events.get(task_id)
        if event is not None:
            await event.wait()

    async def poll_signals(self) -> list[Signal]:
        """Poll fleet-api for pending signals.

        Uses the same ``GET /agents/{id}/tasks/pending`` endpoint as the task
        poller, but extracts the ``signals`` array from the response.  Returns
        an empty list on connection failure.
        """
        path = f"/agents/{self._agent_id}/tasks/pending"
        url = f"{self._fleet_api_url}{path}"
        headers = sign_request(
            method="GET",
            path=path,
            body=b"",
            private_key=self._private_key,
            agent_id=self._agent_id,
        )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            self._current_backoff = min(
                self._current_backoff * 2, _MAX_BACKOFF_SECONDS
            )
            logger.warning(
                "Signal poll failed: %s (backoff %.0fs)", exc, self._current_backoff
            )
            return []

        self._current_backoff = _BASE_BACKOFF_SECONDS

        data = response.json()
        signals_data = data.get("signals", [])
        return [Signal.model_validate(s) for s in signals_data]

    async def run(self, streamer: EventStreamer) -> None:
        """Main signal polling loop.  Runs until cancelled.

        Polls for signals, dispatches them to the appropriate handler,
        and streams acknowledgement events back to fleet-api.
        """
        self._running = True
        try:
            while True:
                try:
                    signals = await self.poll_signals()
                except Exception:
                    logger.exception("Unexpected error during signal poll")
                    signals = []

                for sig in signals:
                    # Build a dedup key from task_id + signal_type + timestamp
                    sig_key = f"{sig.task_id}:{sig.signal_type}:{sig.timestamp}"
                    if sig_key in self._processed_signals:
                        continue

                    await self._handle_signal(sig, streamer)
                    self._processed_signals.add(sig_key)

                # Use backoff on failure, normal interval on success.
                if self._current_backoff > _BASE_BACKOFF_SECONDS:
                    await asyncio.sleep(self._current_backoff)
                else:
                    await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("Signal poller cancelled, shutting down")
            raise
        finally:
            self._running = False

    async def _handle_signal(self, sig: Signal, streamer: EventStreamer) -> None:
        """Dispatch a single signal to the appropriate handler."""
        task_id = sig.task_id

        # Skip signals for tasks we're not tracking.
        if task_id not in self._pause_events:
            logger.debug(
                "Ignoring signal %s for untracked task %s",
                sig.signal_type,
                task_id,
            )
            return

        logger.info("Handling signal %s for task %s", sig.signal_type, task_id)

        if sig.signal_type == "pause_requested":
            await self._handle_pause(task_id, streamer)
        elif sig.signal_type == "resume_requested":
            await self._handle_resume(task_id, streamer)
        elif sig.signal_type == "cancel_requested":
            await self._handle_cancel(task_id, streamer)
        elif sig.signal_type == "redirect_requested":
            await self._handle_redirect(task_id, sig.payload or {}, streamer)
        elif sig.signal_type == "context_injection":
            await self._handle_context_injection(task_id, sig.payload or {}, streamer)
        else:
            logger.warning("Unknown signal type: %s", sig.signal_type)

    async def _handle_pause(self, task_id: str, streamer: EventStreamer) -> None:
        """Handle a pause signal by clearing the pause event (blocking executor)."""
        event = self._pause_events.get(task_id)
        if event is None:
            return

        if not event.is_set():
            logger.debug("Task %s already paused, ignoring duplicate pause", task_id)
            return

        event.clear()
        logger.info("Task %s paused", task_id)

        # Stream status event back to fleet-api.
        ack_event = TaskEvent(
            event_type="status",
            data={"status": "paused"},
            sequence=0,  # Streamer re-sequences
        )
        await self._post_signal_ack(task_id, ack_event, streamer)

    async def _handle_resume(self, task_id: str, streamer: EventStreamer) -> None:
        """Handle a resume signal by setting the pause event (unblocking executor)."""
        event = self._pause_events.get(task_id)
        if event is None:
            return

        if event.is_set():
            logger.debug("Task %s already running, ignoring duplicate resume", task_id)
            return

        event.set()
        logger.info("Task %s resumed", task_id)

        # Stream status event back to fleet-api.
        ack_event = TaskEvent(
            event_type="status",
            data={"status": "running"},
            sequence=0,
        )
        await self._post_signal_ack(task_id, ack_event, streamer)

    async def _handle_cancel(self, task_id: str, streamer: EventStreamer) -> None:
        """Handle a cancel signal by setting the cancel flag."""
        self._cancel_flags[task_id] = True

        # If the task is paused, unblock it so the executor can see the cancel.
        event = self._pause_events.get(task_id)
        if event is not None and not event.is_set():
            event.set()

        logger.info("Task %s cancel requested", task_id)

        # Stream status event back to fleet-api.
        ack_event = TaskEvent(
            event_type="status",
            data={"status": "cancelled"},
            sequence=0,
        )
        await self._post_signal_ack(task_id, ack_event, streamer)

    async def _handle_redirect(
        self,
        task_id: str,
        payload: dict[str, Any],
        streamer: EventStreamer,
    ) -> None:
        """Handle a redirect signal.

        Sets the redirect payload and cancels the current task.  The poller
        will pick up the new task (created server-side by the redirect endpoint)
        on the next poll cycle.
        """
        self._redirect_payloads[task_id] = payload

        # Also flag for cancellation so the executor terminates.
        self._cancel_flags[task_id] = True

        # If paused, unblock so the executor can see the cancel/redirect.
        event = self._pause_events.get(task_id)
        if event is not None and not event.is_set():
            event.set()

        logger.info("Task %s redirect requested", task_id)

        # Stream redirected status back to fleet-api.
        ack_event = TaskEvent(
            event_type="status",
            data={"status": "redirected", "redirect": payload},
            sequence=0,
        )
        await self._post_signal_ack(task_id, ack_event, streamer)

    async def _handle_context_injection(
        self,
        task_id: str,
        payload: dict[str, Any],
        streamer: EventStreamer,
    ) -> None:
        """Handle a context injection signal.

        Queues the context payload for delivery to the executor.  The executor
        picks up pending contexts via ``pop_context()`` between processing steps
        or, if the handler supports it, via stdin write.
        """
        queue = self._context_queue.get(task_id)
        if queue is None:
            logger.warning(
                "Context injection for untracked task %s, ignoring", task_id
            )
            return

        queue.append(payload)
        logger.info(
            "Context injected for task %s (context_sequence=%s)",
            task_id,
            payload.get("context_sequence", "?"),
        )

        # Stream context_injected event back to fleet-api.
        ack_event = TaskEvent(
            event_type="context_injected",
            data={
                "context_type": payload.get("context_type"),
                "context_sequence": payload.get("context_sequence"),
                "status": "delivered",
            },
            sequence=0,
        )
        await self._post_signal_ack(task_id, ack_event, streamer)

    async def _post_signal_ack(
        self,
        task_id: str,
        event: TaskEvent,
        streamer: EventStreamer,
    ) -> None:
        """Post a signal acknowledgement event to fleet-api via the streamer.

        Uses a one-shot async generator to stream a single event.
        """
        try:
            async def _single_event() -> Any:
                yield event

            await streamer.stream(task_id, _single_event())
        except Exception:
            logger.exception(
                "Failed to stream signal ack for task %s (event_type=%s)",
                task_id,
                event.event_type,
            )
