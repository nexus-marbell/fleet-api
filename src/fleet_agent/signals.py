"""Signal poller -- polls fleet-api for pending signals and dispatches them.

State management lives in :mod:`fleet_agent.signal_state`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.models import Signal, TaskEvent
from fleet_agent.signal_state import SignalState
from fleet_agent.signing import sign_request
from fleet_agent.streamer import EventStreamer

logger = logging.getLogger(__name__)
_BASE_BACKOFF = 2.0
_MAX_BACKOFF = 30.0


def _ack(status: str, **extra: Any) -> TaskEvent:
    return TaskEvent(
        event_type="status", data={"status": status, **extra}, sequence=0,
    )


class SignalPoller(SignalState):
    def __init__(
        self, fleet_api_url: str, agent_id: str,
        private_key: Ed25519PrivateKey, interval: int = 2,
    ) -> None:
        super().__init__()
        self._fleet_api_url = fleet_api_url.rstrip("/")
        self._agent_id = agent_id
        self._private_key = private_key
        self._interval = interval
        self._current_backoff = _BASE_BACKOFF
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def poll_signals(self) -> list[Signal]:
        """Poll fleet-api for pending signals. Empty list on failure."""
        path = f"/agents/{self._agent_id}/tasks/pending"
        headers = sign_request(
            method="GET", path=path, body=b"",
            private_key=self._private_key, agent_id=self._agent_id,
        )
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self._fleet_api_url}{path}", headers=headers,
                )
                r.raise_for_status()
        except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
            self._current_backoff = min(self._current_backoff * 2, _MAX_BACKOFF)
            logger.warning("Signal poll failed: %s", exc)
            return []
        self._current_backoff = _BASE_BACKOFF
        return [Signal.model_validate(s) for s in r.json().get("signals", [])]

    async def run(self, streamer: EventStreamer) -> None:
        """Main signal polling loop. Runs until cancelled."""
        self._running = True
        try:
            while True:
                try:
                    sigs = await self.poll_signals()
                except Exception:
                    logger.exception("Signal poll error")
                    sigs = []
                for sig in sigs:
                    key = f"{sig.task_id}:{sig.signal_type}:{sig.timestamp}"
                    if not self.is_signal_processed(key):
                        await self._handle_signal(sig, streamer)
                        self.mark_signal_processed(key)
                await asyncio.sleep(
                    self._current_backoff if self._current_backoff > _BASE_BACKOFF
                    else self._interval
                )
        except asyncio.CancelledError:
            logger.info("Signal poller cancelled, shutting down")
            raise
        finally:
            self._running = False

    async def _handle_signal(self, sig: Signal, streamer: EventStreamer) -> None:
        t = sig.task_id
        if not self.has_task(t):
            return
        logger.info("Handling %s for task %s", sig.signal_type, t)
        if sig.signal_type == "pause_requested":
            await self._handle_pause(t, streamer)
        elif sig.signal_type == "resume_requested":
            await self._handle_resume(t, streamer)
        elif sig.signal_type == "cancel_requested":
            await self._handle_cancel(t, streamer)
        elif sig.signal_type == "redirect_requested":
            await self._handle_redirect(t, sig.payload or {}, streamer)
        elif sig.signal_type == "context_injection":
            self._handle_context_injection(t, sig.payload or {})

    async def _handle_pause(self, t: str, streamer: EventStreamer) -> None:
        ev = self.get_pause_event(t)
        if ev is None or not ev.is_set():
            return
        ev.clear()
        await self._post_ack(t, _ack("paused"), streamer)
    async def _handle_resume(self, t: str, streamer: EventStreamer) -> None:
        ev = self.get_pause_event(t)
        if ev is None or ev.is_set():
            return
        ev.set()
        await self._post_ack(t, _ack("running"), streamer)
    async def _handle_cancel(self, t: str, streamer: EventStreamer) -> None:
        self.set_cancel(t)
        ev = self.get_pause_event(t)
        if ev is not None and not ev.is_set():
            ev.set()
        await self._post_ack(t, _ack("cancelled"), streamer)
    async def _handle_redirect(
        self, t: str, payload: dict[str, Any], streamer: EventStreamer,
    ) -> None:
        self.set_redirect(t, payload)
        self.set_cancel(t)
        ev = self.get_pause_event(t)
        if ev is not None and not ev.is_set():
            ev.set()
        await self._post_ack(t, _ack("redirected", redirect=payload), streamer)

    def _handle_context_injection(self, t: str, payload: dict[str, Any]) -> None:
        queue = self.get_context_queue(t)
        if queue is not None:
            queue.append(payload)

    async def _post_ack(self, t: str, event: TaskEvent, s: EventStreamer) -> None:
        try:
            await s.stream(t, _single_event(event))
        except Exception:
            logger.exception("Ack failed for task %s", t)


async def _single_event(event: TaskEvent) -> AsyncGenerator[TaskEvent, None]:
    yield event
