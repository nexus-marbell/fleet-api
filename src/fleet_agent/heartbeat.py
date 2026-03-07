"""Heartbeat loop -- periodically POSTs to fleet-api to signal liveness.

Runs as a concurrent asyncio task alongside the poller and health server.
Uses exponential backoff on failure, resets on success.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fleet_agent.config import SidecarConfig
from fleet_agent.signing import sign_request

logger = logging.getLogger(__name__)

# Backoff configuration for heartbeat failures.
_BASE_BACKOFF_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 60.0


async def run_heartbeat(config: SidecarConfig, private_key: Ed25519PrivateKey) -> None:
    """Send periodic heartbeats to fleet-api.  Runs until cancelled.

    Parameters
    ----------
    config:
        Sidecar configuration (provides ``fleet_api_url``, ``fleet_agent_id``,
        and ``fleet_heartbeat_interval``).
    private_key:
        The agent's Ed25519 private key used for request signing.
    """
    interval = config.fleet_heartbeat_interval
    path = f"/agents/{config.fleet_agent_id}/heartbeat"
    url = f"{config.fleet_api_url.rstrip('/')}{path}"
    backoff = _BASE_BACKOFF_SECONDS

    try:
        while True:
            headers = sign_request(
                method="POST",
                path=path,
                body=b"",
                private_key=private_key,
                agent_id=config.fleet_agent_id,
            )

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, content=b"", headers=headers)
                    response.raise_for_status()

                # Success -- reset backoff.
                backoff = _BASE_BACKOFF_SECONDS
                logger.debug(
                    "Heartbeat sent for %s", config.fleet_agent_id
                )
            except (httpx.ConnectError, httpx.HTTPStatusError) as exc:
                backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                logger.warning(
                    "Heartbeat failed: %s (backoff %.0fs)", exc, backoff
                )

            await asyncio.sleep(interval if backoff == _BASE_BACKOFF_SECONDS else backoff)
    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled, shutting down")
        raise
