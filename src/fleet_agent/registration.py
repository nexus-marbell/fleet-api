"""Self-registration with fleet-api on sidecar startup.

Posts to ``/agents/register`` with the agent's Ed25519 public key.
Blocks until registration succeeds (retries with exponential backoff).
Idempotent -- fleet-api returns 200 for re-registration with the same key.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from fleet_agent.config import SidecarConfig
from fleet_agent.signing import sign_request

logger = logging.getLogger(__name__)

# Backoff configuration for registration retries.
_BASE_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 60.0
_MAX_RETRIES = 10


async def self_register(config: SidecarConfig, private_key: Ed25519PrivateKey) -> None:
    """Register this agent with fleet-api.  Blocks until successful.

    Parameters
    ----------
    config:
        Sidecar configuration (provides ``fleet_api_url`` and ``fleet_agent_id``).
    private_key:
        The agent's Ed25519 private key (public key is derived from it).
    """
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    public_key_b64 = base64.b64encode(public_key_bytes).decode("utf-8")

    path = "/agents/register"
    url = f"{config.fleet_api_url.rstrip('/')}{path}"

    body_dict = {
        "agent_id": config.fleet_agent_id,
        "public_key": public_key_b64,
        "capabilities": [],
    }

    body = json.dumps(body_dict).encode()

    backoff = _BASE_BACKOFF_SECONDS
    attempt = 0

    while True:
        attempt += 1
        if attempt > _MAX_RETRIES:
            logger.error(
                "Registration failed after %d attempts -- exiting so systemd can restart",
                _MAX_RETRIES,
            )
            raise SystemExit(1)

        headers = sign_request(
            method="POST",
            path=path,
            body=body,
            private_key=private_key,
            agent_id=config.fleet_agent_id,
        )
        headers["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, content=body, headers=headers)

            if response.status_code in (200, 201):
                logger.info(
                    "Registered with fleet-api as %s", config.fleet_agent_id
                )
                return

            # Non-retryable client error (409 = agent_id conflict with different
            # public key, not idempotent re-registration which returns 200)
            if 400 <= response.status_code < 500:
                logger.error(
                    "Registration rejected by fleet-api: %d %s",
                    response.status_code,
                    response.text,
                )
                raise RuntimeError(
                    f"Registration failed with status {response.status_code}: {response.text}"
                )

            # 5xx -- transient, retry
            logger.warning(
                "Registration failed (server error %d), retrying in %.0fs",
                response.status_code,
                backoff,
            )
        except httpx.ConnectError as exc:
            logger.warning(
                "Registration failed (connection error: %s), retrying in %.0fs",
                exc,
                backoff,
            )

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
