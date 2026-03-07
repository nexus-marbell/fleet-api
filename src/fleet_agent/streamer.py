"""Event streamer -- posts task events back to fleet-api."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.models import TaskEvent
from fleet_agent.signing import sign_request

logger = logging.getLogger(__name__)

# Retry configuration for transient failures.
_MAX_RETRIES = 4
_BASE_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 16.0


class EventStreamer:
    """Streams task events back to fleet-api.

    Each event is POSTed to ``/tasks/{task_id}/events`` with Ed25519 signed
    headers.  Transient failures (connection errors, 5xx responses) are retried
    with exponential backoff.

    Failure Mode — Retry Exhaustion
    --------------------------------
    When all retry attempts are exhausted for a given event (default: 4 retries
    with exponential backoff up to 16 s), the event is **dropped**.  The
    streamer logs an ``ERROR``-level message containing the event sequence
    number, task ID, total attempts, and the last error, then continues to the
    next event.

    **What operators should monitor:**

    * Log messages at ``ERROR`` level matching
      ``"Failed to POST event seq=..."`` — each occurrence means one event was
      permanently lost.
    * Sustained occurrences indicate fleet-api is down or the network path is
      broken.

    **Recovery:**

    In Phase 1 there is **no recovery mechanism** for dropped events.  Once
    retries are exhausted the event is gone.  The subprocess stdout that
    produced the event is already consumed and not buffered.  This is a known
    limitation — Phase 2 may introduce a persistent outbox (write-ahead log)
    to allow replay of failed events.
    """

    def __init__(
        self,
        fleet_api_url: str,
        agent_id: str,
        private_key: Ed25519PrivateKey,
    ) -> None:
        self._fleet_api_url = fleet_api_url.rstrip("/")
        self._agent_id = agent_id
        self._private_key = private_key

    async def stream(self, task_id: str, events: AsyncIterator[TaskEvent]) -> None:
        """POST each event from *events* to fleet-api.

        An initial ``status: running`` event is sent before iterating the
        supplied events.  Sequence numbers are auto-incremented starting at 1.
        """
        sequence = 0

        async with httpx.AsyncClient() as client:
            # Send initial "running" status event.
            sequence += 1
            running_event = TaskEvent(
                event_type="status",
                data={"status": "running"},
                sequence=sequence,
            )
            await self._post_event(client, task_id, running_event)

            # Stream execution events.
            async for event in events:
                sequence += 1
                # Re-sequence to guarantee monotonic ordering from the
                # streamer's perspective, regardless of executor numbering.
                posted_event = event.model_copy(update={"sequence": sequence})
                await self._post_event(client, task_id, posted_event)

    async def _post_event(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        event: TaskEvent,
    ) -> None:
        """POST a single event with retries on transient failures."""
        path = f"/tasks/{task_id}/events"
        url = f"{self._fleet_api_url}{path}"
        body = event.model_dump_json().encode()

        backoff = _BASE_BACKOFF_SECONDS
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            headers = sign_request(
                method="POST",
                path=path,
                body=body,
                private_key=self._private_key,
                agent_id=self._agent_id,
            )
            headers["Content-Type"] = "application/json"

            try:
                response = await client.post(url, content=body, headers=headers)
                if response.status_code < 500:
                    if response.status_code >= 400:
                        logger.warning(
                            "fleet-api rejected event seq=%d for task %s: %d %s",
                            event.sequence,
                            task_id,
                            response.status_code,
                            response.text,
                        )
                    return
                # 5xx -- transient, retry.
                last_error = RuntimeError(
                    f"Server error: {response.status_code}"
                )
            except httpx.ConnectError as exc:
                last_error = exc

            if attempt < _MAX_RETRIES:
                logger.info(
                    "Retrying event POST (attempt %d/%d, backoff %.1fs)",
                    attempt + 1,
                    _MAX_RETRIES,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)

        logger.error(
            "Failed to POST event seq=%d for task %s after %d attempts: %s",
            event.sequence,
            task_id,
            _MAX_RETRIES + 1,
            last_error,
        )
