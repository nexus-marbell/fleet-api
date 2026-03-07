"""Fleet Agent sidecar entry point.

Usage::

    python -m fleet_agent

Loads configuration from environment variables, starts the task poller
and the local health endpoint concurrently.
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from fleet_agent.config import SidecarConfig
from fleet_agent.executor import LocalExecutor
from fleet_agent.health import configure as configure_health
from fleet_agent.health import get_app
from fleet_agent.poller import TaskPoller
from fleet_agent.streamer import EventStreamer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fleet_agent")


def _load_private_key(path: str):  # type: ignore[no-untyped-def]
    """Load an Ed25519 private key from a PEM file."""
    with open(path, "rb") as fh:
        key = load_pem_private_key(fh.read(), password=None)
    return key


async def _run_poller(
    config: SidecarConfig,
) -> None:
    """Set up and run the poller + streamer loop."""
    private_key = _load_private_key(config.fleet_agent_private_key_path)

    executor = LocalExecutor()
    streamer = EventStreamer(
        fleet_api_url=config.fleet_api_url,
        agent_id=config.fleet_agent_id,
        private_key=private_key,
    )
    poller = TaskPoller(
        fleet_api_url=config.fleet_api_url,
        agent_id=config.fleet_agent_id,
        private_key=private_key,
        interval=config.fleet_poll_interval,
        max_concurrent=config.fleet_max_concurrent_tasks,
    )

    # Configure health endpoint with runtime references.
    configure_health(
        poller=poller,
        fleet_api_url=config.fleet_api_url,
        agent_id=config.fleet_agent_id,
    )

    await poller.run(executor, streamer)


async def _main() -> None:
    """Start the poller and health server concurrently."""
    config = SidecarConfig()  # type: ignore[call-arg]
    logger.info(
        "Starting fleet agent sidecar: agent=%s api=%s port=%d",
        config.fleet_agent_id,
        config.fleet_api_url,
        config.fleet_sidecar_port,
    )

    health_app = get_app()
    health_config = uvicorn.Config(
        health_app,
        host="0.0.0.0",
        port=config.fleet_sidecar_port,
        log_level="warning",
    )
    health_server = uvicorn.Server(health_config)

    await asyncio.gather(
        _run_poller(config),
        health_server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(_main())
