"""Task poller -- polls fleet-api for pending tasks at a configurable interval."""

from __future__ import annotations

import asyncio
import logging

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.executor import LocalExecutor
from fleet_agent.models import PendingTask
from fleet_agent.signing import sign_request
from fleet_agent.streamer import EventStreamer

logger = logging.getLogger(__name__)

# Backoff configuration for connection failures.
_BASE_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 60.0


class TaskPoller:
    """Polls fleet-api for pending tasks at a configurable interval.

    New tasks are dispatched to a :class:`LocalExecutor` and results are
    streamed back via an :class:`EventStreamer`.  In-flight tasks are tracked
    to avoid double-dispatch, and ``FLEET_MAX_CONCURRENT_TASKS`` is respected.
    """

    def __init__(
        self,
        fleet_api_url: str,
        agent_id: str,
        private_key: Ed25519PrivateKey,
        interval: int = 5,
        max_concurrent: int = 1,
    ) -> None:
        self._fleet_api_url = fleet_api_url.rstrip("/")
        self._agent_id = agent_id
        self._private_key = private_key
        self._interval = interval
        self._max_concurrent = max_concurrent

        self._in_flight: set[str] = set()
        self._running = False
        self._current_backoff = _BASE_BACKOFF_SECONDS

    @property
    def is_running(self) -> bool:
        """Whether the polling loop is currently active."""
        return self._running

    @property
    def active_task_count(self) -> int:
        """Number of tasks currently being executed."""
        return len(self._in_flight)

    async def poll(self) -> list[PendingTask]:
        """GET /agents/{id}/tasks/pending with Ed25519 signed request.

        Returns a list of :class:`PendingTask` objects.  On connection
        failure, returns an empty list and increases the internal backoff.
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
            logger.warning("Poll failed: %s (backoff %.0fs)", exc, self._current_backoff)
            return []

        # Reset backoff on successful connection.
        self._current_backoff = _BASE_BACKOFF_SECONDS

        data = response.json()
        tasks_data = data if isinstance(data, list) else data.get("tasks", [])
        return [PendingTask.model_validate(t) for t in tasks_data]

    async def run(self, executor: LocalExecutor, streamer: EventStreamer) -> None:
        """Main polling loop.

        Polls for pending tasks, dispatches new ones to *executor*, and
        streams results back via *streamer*.  Runs until cancelled.
        """
        self._running = True
        try:
            while True:
                try:
                    tasks = await self.poll()
                except Exception:
                    logger.exception("Unexpected error during poll")
                    tasks = []

                for task in tasks:
                    if task.task_id in self._in_flight:
                        continue
                    if len(self._in_flight) >= self._max_concurrent:
                        logger.debug(
                            "At concurrency limit (%d), skipping remaining tasks",
                            self._max_concurrent,
                        )
                        break
                    self._in_flight.add(task.task_id)
                    asyncio.create_task(
                        self._dispatch(task, executor, streamer),
                        name=f"fleet-task-{task.task_id}",
                    )

                # Use backoff interval on failure, normal interval on success.
                if self._current_backoff > _BASE_BACKOFF_SECONDS:
                    await asyncio.sleep(self._current_backoff)
                else:
                    await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            logger.info("Poller cancelled, shutting down")
            raise
        finally:
            self._running = False

    async def _dispatch(
        self,
        task: PendingTask,
        executor: LocalExecutor,
        streamer: EventStreamer,
    ) -> None:
        """Execute a single task and stream events back."""
        logger.info("Dispatching task %s (workflow %s)", task.task_id, task.workflow_id)
        try:
            events = executor.execute(task)
            await streamer.stream(task.task_id, events)
        except Exception:
            logger.exception("Unhandled error dispatching task %s", task.task_id)
        finally:
            self._in_flight.discard(task.task_id)
            logger.info("Task %s completed, removed from in-flight", task.task_id)
