"""Local task executor -- dispatches tasks to the agent orchestrator via subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fleet_agent.models import PendingTask, TaskEvent

logger = logging.getLogger(__name__)

# Default timeout when the task does not specify one (10 minutes).
_DEFAULT_TIMEOUT_SECONDS = 600


class ExecutionError(Exception):
    """Raised when a subprocess execution fails in a non-recoverable way."""


class LocalExecutor:
    """Dispatches tasks to the local agent orchestrator via subprocess.

    Phase 1 implementation: runs a workflow handler as an async subprocess.
    Task input is passed as JSON on stdin.  The handler writes newline-delimited
    JSON events to stdout (one ``TaskEvent``-shaped object per line).
    """

    def __init__(self, handler_command: str = "fleet-handler") -> None:
        self._handler_command = handler_command

    async def execute(self, task: PendingTask) -> AsyncGenerator[TaskEvent, None]:
        """Execute *task* and yield events as they occur.

        The handler subprocess receives the full task input as JSON on stdin.
        Each line of stdout is parsed as a JSON event and yielded as a
        :class:`TaskEvent`.

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
                self._handler_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            sequence += 1
            yield TaskEvent(
                event_type="failed",
                data={"error": f"Handler command not found: {self._handler_command}"},
                sequence=sequence,
            )
            raise ExecutionError(
                f"Handler command not found: {self._handler_command}"
            ) from exc

        # Feed stdin and close it so the handler can start processing.
        if process.stdin is not None:
            process.stdin.write(stdin_payload)
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

        try:
            async for event in self._read_events(process, sequence, timeout):
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
    ) -> AsyncGenerator[TaskEvent, None]:
        """Read newline-delimited JSON events from the subprocess stdout."""
        sequence = start_sequence
        if process.stdout is None:
            return

        async with asyncio.timeout(timeout):
            while True:
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
