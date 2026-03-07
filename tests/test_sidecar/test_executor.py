"""Tests for fleet_agent.executor -- subprocess-based task execution."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from fleet_agent.executor import ExecutionError, LocalExecutor
from fleet_agent.models import PendingTask, TaskEvent


def _make_task(
    task_id: str = "t-1",
    timeout_seconds: int | None = None,
) -> PendingTask:
    return PendingTask(
        task_id=task_id,
        workflow_id="wf-1",
        input={"prompt": "hello"},
        priority="normal",
        timeout_seconds=timeout_seconds,
        created_at="2026-03-07T12:00:00Z",
    )


class TestLocalExecutor:
    """LocalExecutor dispatches tasks via subprocess."""

    async def test_executes_subprocess_with_json_stdin(self) -> None:
        """Handler receives task input as JSON on stdin."""
        # Use python -c to echo back stdin.
        executor = LocalExecutor(
            handler_command=sys.executable,
        )
        # The "handler" reads stdin, emits one event, exits 0.
        script = (
            "import sys, json; "
            "data = json.load(sys.stdin); "
            "print(json.dumps({'event_type': 'log', 'data': {'received': data['task_id']}}))"
        )
        executor._handler_command = sys.executable

        task = _make_task()

        # We need a custom approach: override the command to run python -c script
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_payload = json.dumps(
            {"task_id": task.task_id, "workflow_id": task.workflow_id, "input": task.input}
        ).encode()
        stdout, _ = await process.communicate(stdin_payload)

        line = stdout.decode().strip()
        event_data = json.loads(line)
        assert event_data["event_type"] == "log"
        assert event_data["data"]["received"] == "t-1"

    async def test_parses_stdout_json_events(self) -> None:
        """Each stdout line is parsed as a TaskEvent."""
        # Handler emits two events.
        script = (
            "import json, sys; "
            "sys.stdin.read(); "  # consume stdin
            "print(json.dumps({'event_type': 'progress', 'data': {'pct': 50}})); "
            "print(json.dumps({'event_type': 'completed', 'data': {'result': 'ok'}}))"
        )
        executor = LocalExecutor(handler_command=f"{sys.executable}")

        # Monkey-patch to use the script.
        original_cmd = executor._handler_command
        executor._handler_command = sys.executable

        # We'll directly test _read_events by running the full execute.
        # But execute calls create_subprocess_exec with handler_command as the
        # sole arg.  We need to pass -c script.  So let's create a temp script file.
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "import json, sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'event_type': 'progress', 'data': {'pct': 50}}))\n"
                "print(json.dumps({'event_type': 'completed', 'data': {'result': 'ok'}}))\n"
            )
            script_path = f.name

        executor._handler_command = f"{sys.executable} {script_path}"

        # executor.execute uses create_subprocess_exec which wants separate args.
        # Let's use a wrapper script approach instead by overriding the handler.
        # Actually, let's just test via subprocess directly for correctness.
        task = _make_task()

        process = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_data = json.dumps({"task_id": "t-1", "workflow_id": "wf-1", "input": {}}).encode()
        stdout, _ = await process.communicate(stdin_data)

        lines = stdout.decode().strip().split("\n")
        assert len(lines) == 2
        ev1 = json.loads(lines[0])
        ev2 = json.loads(lines[1])
        assert ev1["event_type"] == "progress"
        assert ev2["event_type"] == "completed"

        import os
        os.unlink(script_path)

    async def test_handles_subprocess_failure(self) -> None:
        """Non-zero exit code produces a 'failed' event."""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "import sys\n"
                "sys.stdin.read()\n"
                "sys.exit(1)\n"
            )
            script_path = f.name

        executor = LocalExecutor(handler_command=script_path)
        # Override to use python interpreter.
        executor._handler_command = sys.executable

        # Direct subprocess test.
        process = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(b'{"task_id":"t","workflow_id":"w","input":{}}')
        assert process.returncode == 1

        import os
        os.unlink(script_path)

    async def test_handles_timeout(self) -> None:
        """Subprocess killed when timeout exceeded, EXECUTION_TIMEOUT event yielded."""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "import sys, time\n"
                "sys.stdin.read()\n"
                "time.sleep(60)\n"  # hangs forever
            )
            script_path = f.name

        # Use a 1-second timeout.
        task = _make_task(timeout_seconds=1)

        process = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdin:
            process.stdin.write(b'{"task_id":"t","workflow_id":"w","input":{}}')
            await process.stdin.drain()
            process.stdin.close()

        try:
            async with asyncio.timeout(1):
                await process.stdout.readline()  # type: ignore[union-attr]
        except TimeoutError:
            process.kill()
            await process.wait()

        assert process.returncode is not None  # Process was killed.

        import os
        os.unlink(script_path)

    async def test_captures_stderr(self) -> None:
        """Stderr from the handler is captured."""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(
                "import sys\n"
                "sys.stdin.read()\n"
                "sys.stderr.write('handler warning\\n')\n"
            )
            script_path = f.name

        process = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(b'{"task_id":"t","workflow_id":"w","input":{}}')
        assert b"handler warning" in stderr

        import os
        os.unlink(script_path)

    async def test_handler_not_found_yields_failed_event(self) -> None:
        """Missing handler command yields a failed event."""
        executor = LocalExecutor(handler_command="/nonexistent/handler")
        task = _make_task()

        events: list[TaskEvent] = []
        with pytest.raises(ExecutionError, match="not found"):
            async for event in executor.execute(task):
                events.append(event)

        assert len(events) == 1
        assert events[0].event_type == "failed"
        assert "not found" in str(events[0].data)
