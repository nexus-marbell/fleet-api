"""Tests for fleet_agent.heartbeat -- periodic heartbeat to fleet-api."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.config import SidecarConfig
from fleet_agent.heartbeat import run_heartbeat


def _make_config(monkeypatch: pytest.MonkeyPatch, interval: int = 1) -> SidecarConfig:
    """Create a SidecarConfig with test values."""
    monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
    monkeypatch.setenv("FLEET_AGENT_ID", "test-agent")
    monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
    monkeypatch.setenv("FLEET_EXECUTOR_COMMAND", "fleet-handler")
    monkeypatch.setenv("FLEET_HEARTBEAT_INTERVAL", str(interval))
    return SidecarConfig()  # type: ignore[call-arg]


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


_DUMMY_REQUEST = httpx.Request("POST", "https://fleet.example.com/agents/test-agent/heartbeat")


def _ok_response(status_code: int = 200) -> httpx.Response:
    """Create a heartbeat success response."""
    resp = httpx.Response(
        status_code,
        json={"agent_id": "test-agent", "status": "active"},
    )
    resp._request = _DUMMY_REQUEST
    return resp


class TestHeartbeatLoop:
    """run_heartbeat sends periodic heartbeats."""

    async def test_sends_heartbeat_at_interval(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Heartbeat POSTs to /agents/{id}/heartbeat at configured interval."""
        config = _make_config(monkeypatch, interval=1)
        mock_response = _ok_response()
        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise asyncio.CancelledError
            return mock_response

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)

            assert call_count == 3
            # First two successes should sleep at the configured interval.
            assert mock_sleep.call_count == 2
            for call in mock_sleep.call_args_list:
                assert call[0][0] == 1  # interval=1

    async def test_posts_to_correct_url_with_auth(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Heartbeat request targets the correct URL with signed headers."""
        config = _make_config(monkeypatch, interval=1)
        mock_response = _ok_response()
        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            return mock_response

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)

            call_args = mock_client.post.call_args_list[0]
            assert "/agents/test-agent/heartbeat" in call_args[0][0]
            headers = call_args[1]["headers"]
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Signature test-agent:")
            assert "X-Fleet-Timestamp" in headers

    async def test_handles_cancellation_gracefully(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """CancelledError during sleep propagates cleanly."""
        config = _make_config(monkeypatch, interval=1)
        mock_response = _ok_response()

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Cancel after first sleep.
            mock_sleep.side_effect = asyncio.CancelledError

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)


class TestHeartbeatBackoff:
    """Heartbeat exponential backoff on failure."""

    async def test_backoff_on_connection_error(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Connection errors increase backoff."""
        config = _make_config(monkeypatch, interval=1)
        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise asyncio.CancelledError
            raise httpx.ConnectError("refused")

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)

            # Backoff should increase on failures.
            assert mock_sleep.call_count == 2
            sleep_values = [c[0][0] for c in mock_sleep.call_args_list]
            # First failure: 30*2=60, second: min(60*2,60)=60
            assert sleep_values[0] == 60.0
            assert sleep_values[1] == 60.0

    async def test_backoff_resets_on_success(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Successful heartbeat resets backoff to base."""
        config = _make_config(monkeypatch, interval=1)
        mock_response = _ok_response()
        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            if call_count >= 4:
                raise asyncio.CancelledError
            return mock_response

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)

            assert call_count == 4
            sleep_values = [c[0][0] for c in mock_sleep.call_args_list]
            # 1st: failure -> backoff=60 (sleep 60)
            # 2nd: success -> backoff reset to 30 (sleep interval=1)
            # 3rd: success -> backoff still 30 (sleep interval=1)
            assert sleep_values[0] == 60.0
            assert sleep_values[1] == 1  # interval, since backoff reset
            assert sleep_values[2] == 1

    async def test_backoff_on_http_status_error(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """HTTP status errors trigger backoff."""
        config = _make_config(monkeypatch, interval=1)
        call_count = 0

        error_resp = httpx.Response(503, json={"error": "unavailable"})
        error_resp._request = _DUMMY_REQUEST

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            return error_resp

        with (
            patch("fleet_agent.heartbeat.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.heartbeat.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(asyncio.CancelledError):
                await run_heartbeat(config, private_key)

            assert mock_sleep.call_count == 1
            # raise_for_status triggers HTTPStatusError -> backoff doubles from 30 to 60
            assert mock_sleep.call_args_list[0][0][0] == 60.0
