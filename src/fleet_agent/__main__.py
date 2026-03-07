"""Fleet Agent sidecar entry point.

Usage::

    python -m fleet_agent

Loads configuration from environment variables, self-registers with fleet-api,
then starts the task poller, signal poller, heartbeat loop, and local health
endpoint concurrently.  Handles SIGTERM/SIGINT for graceful shutdown.

Phase 2 addition (Unit 8): the signal poller runs concurrently with the task
poller, checking for pause/resume/cancel/redirect/context injection signals
at ``fleet_signal_poll_interval`` (default: 2 seconds).
"""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from fleet_agent.config import SidecarConfig
from fleet_agent.executor import LocalExecutor
from fleet_agent.health import configure as configure_health
from fleet_agent.health import get_app
from fleet_agent.heartbeat import run_heartbeat
from fleet_agent.poller import TaskPoller
from fleet_agent.registration import self_register
from fleet_agent.signals import SignalPoller
from fleet_agent.streamer import EventStreamer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fleet_agent")


def _load_private_key(path: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM file."""
    with open(path, "rb") as fh:
        key = load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError(
            f"Expected Ed25519 private key, got {type(key).__name__}"
        )
    return key


async def _run_poller(
    config: SidecarConfig,
    private_key: Ed25519PrivateKey,
) -> None:
    """Set up and run the poller + streamer + signal poller loop."""
    executor = LocalExecutor(handler_command=config.fleet_executor_command)
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
    signal_poller = SignalPoller(
        fleet_api_url=config.fleet_api_url,
        agent_id=config.fleet_agent_id,
        private_key=private_key,
        interval=config.fleet_signal_poll_interval,
    )

    # Configure health endpoint with runtime references.
    configure_health(
        poller=poller,
        fleet_api_url=config.fleet_api_url,
        agent_id=config.fleet_agent_id,
    )

    # Run task poller and signal poller concurrently.
    # The task poller handles task assignment and execution.
    # The signal poller handles control signals for in-flight tasks.
    await asyncio.gather(
        poller.run(executor, streamer, signal_poller=signal_poller),
        signal_poller.run(streamer),
    )


async def _main() -> None:
    """Self-register, then start the poller, heartbeat, and health server concurrently."""
    config = SidecarConfig()
    private_key = _load_private_key(config.fleet_agent_private_key_path)

    logger.info(
        "Starting fleet agent sidecar: agent=%s api=%s port=%d signal_poll=%ds",
        config.fleet_agent_id,
        config.fleet_api_url,
        config.fleet_sidecar_port,
        config.fleet_signal_poll_interval,
    )

    # 1. Self-register (blocks until successful).
    await self_register(config, private_key)

    # 2. Concurrent: poller (+ signal poller) + heartbeat + health.
    health_app = get_app()
    health_config = uvicorn.Config(
        health_app,
        host="0.0.0.0",
        port=config.fleet_sidecar_port,
        log_level="warning",
    )
    health_server = uvicorn.Server(health_config)

    loop = asyncio.get_running_loop()
    gather_task: asyncio.Future[tuple[None, None, None]] | None = None

    def _shutdown_handler() -> None:
        logger.info("Shutting down fleet-agent sidecar")
        if gather_task is not None:
            gather_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown_handler)

    gather_task = asyncio.ensure_future(
        asyncio.gather(
            _run_poller(config, private_key),
            run_heartbeat(config, private_key),
            health_server.serve(),
        )
    )

    try:
        await gather_task
    except asyncio.CancelledError:
        logger.info("Fleet-agent sidecar shut down complete")


if __name__ == "__main__":
    asyncio.run(_main())
