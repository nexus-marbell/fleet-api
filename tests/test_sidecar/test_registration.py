"""Tests for fleet_agent.registration -- self-registration with fleet-api."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from fleet_agent.config import SidecarConfig
from fleet_agent.registration import self_register


def _make_config(monkeypatch: pytest.MonkeyPatch) -> SidecarConfig:
    """Create a SidecarConfig with test values."""
    monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
    monkeypatch.setenv("FLEET_AGENT_ID", "test-agent")
    monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
    monkeypatch.setenv("FLEET_EXECUTOR_COMMAND", "fleet-handler")
    return SidecarConfig()  # type: ignore[call-arg]


@pytest.fixture
def private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _ok_response(status_code: int, data: object) -> httpx.Response:
    """Create a response with a request set."""
    resp = httpx.Response(status_code, json=data)
    resp._request = httpx.Request("POST", "https://fleet.example.com/agents/register")
    return resp


class TestSelfRegisterSuccess:
    """self_register succeeds on 200 or 201."""

    async def test_registers_on_201(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """201 Created means new registration -- function returns."""
        config = _make_config(monkeypatch)
        mock_response = _ok_response(201, {"agent_id": "test-agent", "status": "registered"})

        with patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/agents/register" in call_args[0][0]

    async def test_registers_on_200_idempotent(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """200 OK means idempotent re-registration -- function returns."""
        config = _make_config(monkeypatch)
        mock_response = _ok_response(200, {"agent_id": "test-agent", "status": "registered"})

        with patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            mock_client.post.assert_called_once()

    async def test_sends_correct_public_key(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Request body contains the base64-encoded public key derived from private key."""
        config = _make_config(monkeypatch)
        mock_response = _ok_response(201, {"agent_id": "test-agent"})

        expected_pub = base64.b64encode(
            private_key.public_key().public_bytes(
                encoding=Encoding.Raw, format=PublicFormat.Raw
            )
        ).decode("utf-8")

        with patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            call_args = mock_client.post.call_args
            import json

            body = json.loads(call_args[1]["content"])
            assert body["agent_id"] == "test-agent"
            assert body["public_key"] == expected_pub
            assert body["capabilities"] == []

    async def test_sends_signed_request(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Request includes Authorization and X-Fleet-Timestamp headers."""
        config = _make_config(monkeypatch)
        mock_response = _ok_response(201, {"agent_id": "test-agent"})

        with patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            call_args = mock_client.post.call_args
            headers = call_args[1]["headers"]
            assert "Authorization" in headers
            assert headers["Authorization"].startswith("Signature test-agent:")
            assert "X-Fleet-Timestamp" in headers
            assert headers["Content-Type"] == "application/json"


class TestSelfRegisterRetry:
    """self_register retries on connection errors and 5xx."""

    async def test_retries_on_connection_error(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Connection errors trigger retry, then success completes."""
        config = _make_config(monkeypatch)
        mock_response = _ok_response(201, {"agent_id": "test-agent"})

        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            return mock_response

        with (
            patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.registration.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            assert call_count == 2
            mock_sleep.assert_called_once()

    async def test_retries_on_server_error(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """5xx responses trigger retry, then success completes."""
        config = _make_config(monkeypatch)
        error_response = _ok_response(503, {"error": "Service unavailable"})
        success_response = _ok_response(201, {"agent_id": "test-agent"})

        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return error_response
            return success_response

        with (
            patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.registration.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            assert call_count == 2
            mock_sleep.assert_called_once()

    async def test_raises_on_client_error(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """4xx client errors raise immediately (no retry)."""
        config = _make_config(monkeypatch)
        error_response = _ok_response(409, {"error": "Agent already exists with different key"})

        with patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = error_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(RuntimeError, match="Registration failed with status 409"):
                await self_register(config, private_key)

            # Only one attempt -- no retry on 4xx.
            mock_client.post.assert_called_once()

    async def test_exponential_backoff_on_retry(
        self, monkeypatch: pytest.MonkeyPatch, private_key: Ed25519PrivateKey
    ) -> None:
        """Backoff doubles on each retry."""
        config = _make_config(monkeypatch)
        success_response = _ok_response(201, {"agent_id": "test-agent"})

        call_count = 0

        async def _post_side_effect(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                raise httpx.ConnectError("refused")
            return success_response

        with (
            patch("fleet_agent.registration.httpx.AsyncClient") as mock_client_cls,
            patch("fleet_agent.registration.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            mock_client = AsyncMock()
            mock_client.post.side_effect = _post_side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await self_register(config, private_key)

            assert call_count == 4
            assert mock_sleep.call_count == 3
            # Verify exponential backoff: 5, 10, 20
            sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
            assert sleep_calls == [5.0, 10.0, 20.0]
