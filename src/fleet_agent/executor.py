"""Local task executor -- dispatches tasks to the agent orchestrator via subprocess.

Phase 2 enhancement (Unit 8, RFC 1 §7.2 items 5-7): the executor is now
interruptible.  A :class:`SignalPoller` can pause, resume, cancel, redirect,
and inject context into running tasks.  The executor checks for these signals
between reading each line of subprocess output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from fleet_agent.models import PendingTask, TaskEvent

if TYPE_CHECKING:
    from fleet_agent.signals import SignalPoller

logger = logging.getLogger(__name__)

# Default timeout when the task does not specify one (10 minutes).
_DEFAULT_TIMEOUT_SECONDS = 600


class ExecutionError(Exception):
    """Raised when a subprocess execution fails in a non-recoverable way."""


class TaskCancelledError(Exception):
    """Raised when a task is cancelled via a signal."""


class TaskRedirectedError(Exception):
    """Raised when a task is redirected via a signal.

    Attributes:
        redirect_payload: The redirect parameters from the signal.
    """

    def __init__(self, redirect_payload: dict[str, Any]) -> None:
        self.redirect_payload = redirect_payload
        super().__init__("Task redirected")


class LocalExecutor:
    """Dispatches tasks to the local agent orchestrator via subprocess.

    Phase 1 implementation: runs a workflow handler as an async subprocess.
    Task input is passed as JSON on stdin.  The handler writes newline-delimited
    JSON events to stdout (one ``TaskEvent``-shaped object per line).

    Phase 2 addition: if a ``signal_poller`` is provided, the executor checks
    for pause/cancel/redirect/context signals between each line of output,
    making the execution interruptible.
    """

    def __init__(self, handler_command: str = "fleet-handler") -> None:
        self._handler_command = handler_command

    async def execute(
        self,
        task: PendingTask,
        signal_poller: SignalPoller | None = None,
    ) -> AsyncGenerator[TaskEvent, None]:
        """Execute *task* and yield events as they occur.

        The handler subprocess receives the full task input as JSON on stdin.
        Each line of stdout is parsed as a JSON event and yielded as a
        :class:`TaskEvent`.

        If *signal_poller* is provided, the executor checks for pending signals
        between reading each line:

        - **Pause**: blocks until resumed (via ``wait_if_paused``).
        - **Cancel**: terminates the subprocess and yields a ``cancelled`` event.
        - **Redirect**: terminates the subprocess and raises :class:`TaskRedirected`.
        - **Context injection**: yields ``context_injected`` events inline.

        If the subprocess exits with a non-zero code, a ``failed`` event is
        yielded.  If ``timeout_seconds`` is exceeded, the subprocess is killed
        and an ``EXECUTION_TIMEOUT`` event is yielded.
        """
        timeout = task.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS
        stdin_payload = json.dumps(
            {"task_id": task.task_id, "workflow_id": task.workflow_id, "input": task.input}
        ).encode()

        sequence = 0

        try:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(self._handler_command),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            sequence += 1
            resolved = shlex.split(self._handler_command)
            yield TaskEvent(
                event_type="failed",
                data={
                    "error": (
                        f"Handler command not found: {self._handler_command!r}"
                        f" (resolved: {resolved})"
                    )
                },
                sequence=sequence,
            )
            raise ExecutionError(
                f"Handler command not found: {self._handler_command!r}"
                f" (resolved: {resolved})"
            ) from exc

        # Feed stdin and close it so the handler can start processing.
        if process.stdin is not None:
            process.stdin.write(stdin_payload)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

        try:
            async for event in self._read_events(
                process, sequence, timeout, task.task_id, signal_poller
            ):
                sequence = event.sequence
                yield event
        except TimeoutError:
            process.kill()
            await process.wait()
            stderr_bytes = b""
            if process.stderr is not None:
                stderr_bytes = await process.stderr.read()
            sequence += 1
            yield TaskEvent(
                event_type="failed",
                data={
                    "error": "EXECUTION_TIMEOUT",
                    "detail": f"Task exceeded {timeout}s timeout",
                    "stderr": stderr_bytes.decode(errors="replace"),
                },
                sequence=sequence,
            )
            return
        except TaskCancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            sequence += 1
            yield TaskEvent(
                event_type="status",
                data={"status": "cancelled", "reason": "Cancelled by signal"},
                sequence=sequence,
            )
            return
        except TaskRedirectedError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
            # Don't yield a terminal event — the signal poller already streamed
            # the redirected status.  The poller will pick up the new task.
            return

        # Wait for the process to exit.
        await process.wait()

        stderr_bytes = b""
        if process.stderr is not None:
            stderr_bytes = await process.stderr.read()
        stderr_text = stderr_bytes.decode(errors="replace")

        if stderr_text:
            logger.info("Handler stderr for task %s: %s", task.task_id, stderr_text)

        if process.returncode != 0:
            sequence += 1
            yield TaskEvent(
                event_type="failed",
                data={
                    "error": "SUBPROCESS_FAILED",
                    "exit_code": process.returncode,
                    "stderr": stderr_text,
                },
                sequence=sequence,
            )

    async def _read_events(
        self,
        process: asyncio.subprocess.Process,
        start_sequence: int,
        timeout: int,
        task_id: str = "",
        signal_poller: SignalPoller | None = None,
    ) -> AsyncGenerator[TaskEvent, None]:
        """Read newline-delimited JSON events from the subprocess stdout.

        Between each line read, checks the signal poller (if provided) for:
        - Pause signals: blocks until resumed.
        - Cancel signals: raises :class:`TaskCancelled`.
        - Redirect signals: raises :class:`TaskRedirected`.
        - Context injection: yields inline ``context_injected`` events.
        """
        sequence = start_sequence
        if process.stdout is None:
            return

        async with asyncio.timeout(timeout):
            while True:
                # Check signals before reading the next line.
                if signal_poller is not None and task_id:
                    # Check for cancel/redirect first (highest priority).
                    redirect = signal_poller.get_redirect(task_id)
                    if redirect is not None:
                        raise TaskRedirectedError(redirect)

                    if signal_poller.is_cancelled(task_id):
                        raise TaskCancelledError()

                    # Block if paused (resumes when signal poller sets the event).
                    await signal_poller.wait_if_paused(task_id)

                    # Yield any pending context injections inline.
                    contexts = signal_poller.pop_context(task_id)
                    for ctx in contexts:
                        sequence += 1
                        yield TaskEvent(
                            event_type="context_injected",
                            data={
                                "context_type": ctx.get("context_type"),
                                "context_sequence": ctx.get("context_sequence"),
                                "payload": ctx.get("payload"),
                                "urgency": ctx.get("urgency", "normal"),
                            },
                            sequence=sequence,
                        )

                line = await process.stdout.readline()
                if not line:
                    break
                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event_data = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON line from handler: %s", line_str)
                    continue

                sequence += 1
                yield TaskEvent(
                    event_type=event_data.get("event_type", "log"),
                    data=event_data.get("data"),
                    sequence=sequence,
                )
