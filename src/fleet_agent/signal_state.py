"""Signal state management -- shared state for signal-executor coordination.

Extracted from signals.py (PR #47 review) to satisfy SRP: this module owns
the shared data structures and state accessors used to coordinate between
the signal poller and the task executor.

State managed here:
- Pause events (asyncio.Event per task -- clear=paused, set=running)
- Cancel flags (bool per task)
- Redirect payloads (dict per task)
- Context injection queues (list per task)
- Processed signal deduplication set
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SignalState:
    """Shared state for signal-executor coordination.

    Owns all mutable state that the signal poller writes and the executor
    reads.  Thread-safe within a single asyncio event loop (all access is
    from coroutines on the same loop).
    """

    def __init__(self) -> None:
        # Maps task_id -> asyncio.Event for pause/resume.
        # When a pause signal arrives, the event is cleared (blocking the executor).
        # When a resume signal arrives, the event is set (unblocking the executor).
        self._pause_events: dict[str, asyncio.Event] = {}

        # Maps task_id -> bool indicating cancellation requested.
        self._cancel_flags: dict[str, bool] = {}

        # Maps task_id -> redirect payload (new_input, reason, etc.).
        self._redirect_payloads: dict[str, dict[str, Any]] = {}

        # Maps task_id -> list of pending context payloads.
        self._context_queue: dict[str, list[dict[str, Any]]] = {}

        # Track which signals have been processed to avoid re-processing.
        self._processed_signals: set[str] = set()

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
        """Clean up signal state for a completed/failed task.

        Also prunes entries from ``_processed_signals`` that belong to this
        task (keyed as ``task_id:signal_type:timestamp``), preventing
        unbounded growth of the deduplication set over the sidecar's lifetime.
        """
        self._pause_events.pop(task_id, None)
        self._cancel_flags.pop(task_id, None)
        self._redirect_payloads.pop(task_id, None)
        self._context_queue.pop(task_id, None)

        # Prune processed signals for this task (prefix match on composite key).
        prefix = f"{task_id}:"
        self._processed_signals = {
            key for key in self._processed_signals if not key.startswith(prefix)
        }

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

    def has_task(self, task_id: str) -> bool:
        """Check if a task is registered for signal monitoring."""
        return task_id in self._pause_events

    def get_pause_event(self, task_id: str) -> asyncio.Event | None:
        """Get the pause event for a task (used by signal handlers)."""
        return self._pause_events.get(task_id)

    def set_cancel(self, task_id: str) -> None:
        """Set the cancel flag for a task."""
        self._cancel_flags[task_id] = True

    def set_redirect(self, task_id: str, payload: dict[str, Any]) -> None:
        """Set the redirect payload for a task."""
        self._redirect_payloads[task_id] = payload

    def get_context_queue(self, task_id: str) -> list[dict[str, Any]] | None:
        """Get the context queue for a task (None if unregistered)."""
        return self._context_queue.get(task_id)

    def is_signal_processed(self, sig_key: str) -> bool:
        """Check if a signal has already been processed."""
        return sig_key in self._processed_signals

    def mark_signal_processed(self, sig_key: str) -> None:
        """Mark a signal as processed."""
        self._processed_signals.add(sig_key)
