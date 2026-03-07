"""Tests for graceful shutdown in fleet_agent.__main__."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


class TestGracefulShutdown:
    """SIGTERM/SIGINT trigger graceful shutdown."""

    async def test_sigterm_cancels_gather_tasks(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """SIGTERM handler cancels the gather task, allowing clean shutdown."""
        monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
        monkeypatch.setenv("FLEET_AGENT_ID", "test-agent")
        monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
        monkeypatch.setenv("FLEET_EXECUTOR_COMMAND", "fleet-handler")
        monkeypatch.setenv("FLEET_HEARTBEAT_INTERVAL", "1")

        from fleet_agent.__main__ import _main

        registered_handlers: dict[int, object] = {}

        def _capture_signal_handler(sig: int, handler: object) -> None:
            registered_handlers[sig] = handler

        with (
            patch("fleet_agent.__main__._load_private_key", return_value=private_key),
            patch("fleet_agent.__main__.self_register", new_callable=AsyncMock),
            patch("fleet_agent.__main__._run_poller", new_callable=AsyncMock) as mock_poller,
            patch("fleet_agent.__main__.run_heartbeat", new_callable=AsyncMock) as mock_heartbeat,
            patch("fleet_agent.__main__.get_app") as mock_get_app,
            patch("fleet_agent.__main__.uvicorn") as mock_uvicorn,
        ):
            # Configure uvicorn mock.
            mock_server = MagicMock()
            mock_server.serve = AsyncMock()
            mock_uvicorn.Config.return_value = MagicMock()
            mock_uvicorn.Server.return_value = mock_server

            # Make poller block until cancelled.
            async def _block_until_cancelled(*args: object, **kwargs: object) -> None:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass

            mock_poller.side_effect = _block_until_cancelled
            mock_heartbeat.side_effect = _block_until_cancelled
            mock_server.serve.side_effect = _block_until_cancelled

            loop = asyncio.get_event_loop()
            original_add = loop.add_signal_handler

            def _patched_add(sig: int, handler: object, *args: object) -> None:
                registered_handlers[sig] = handler

            monkeypatch.setattr(loop, "add_signal_handler", _patched_add)

            # Run _main in a task, then trigger SIGTERM handler.
            main_task = asyncio.create_task(_main())

            # Give the event loop a moment to start.
            await asyncio.sleep(0.05)

            # Trigger the registered SIGTERM handler.
            if signal.SIGTERM in registered_handlers:
                registered_handlers[signal.SIGTERM]()  # type: ignore[operator]

            # Wait for _main to complete after cancellation.
            await asyncio.wait_for(main_task, timeout=2.0)

            # Verify signal handlers were registered.
            assert signal.SIGTERM in registered_handlers
            assert signal.SIGINT in registered_handlers

    async def test_poller_handles_cancelled_error(self) -> None:
        """TaskPoller.run handles CancelledError gracefully."""
        from fleet_agent.poller import TaskPoller

        pk = Ed25519PrivateKey.generate()
        poller = TaskPoller(
            fleet_api_url="https://fleet.example.com",
            agent_id="test-agent",
            private_key=pk,
            interval=1,
            max_concurrent=1,
        )

        executor = AsyncMock()
        streamer = AsyncMock()

        with patch("fleet_agent.poller.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = asyncio.CancelledError
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await poller.run(executor, streamer)

        # Poller should report as not running after cancellation.
        assert not poller.is_running

    async def test_heartbeat_handles_cancelled_error(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """run_heartbeat handles CancelledError gracefully."""
        monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
        monkeypatch.setenv("FLEET_AGENT_ID", "test-agent")
        monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
        monkeypatch.setenv("FLEET_EXECUTOR_COMMAND", "fleet-handler")
        monkeypatch.setenv("FLEET_HEARTBEAT_INTERVAL", "1")

        from fleet_agent.config import SidecarConfig
        from fleet_agent.heartbeat import run_heartbeat

        config = SidecarConfig()  # type: ignore[call-arg]

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = asyncio.CancelledError
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)
