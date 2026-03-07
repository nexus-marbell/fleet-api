"""Sidecar configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class SidecarConfig(BaseSettings):
    """Configuration for the fleet agent sidecar.

    All values are read from environment variables with the exact names shown
    (no prefix stripping).  Only ``fleet_api_url``, ``fleet_agent_id``, and
    ``fleet_agent_private_key_path`` are required; the rest have sensible
    defaults.
    """

    fleet_api_url: str
    fleet_agent_id: str
    fleet_agent_private_key_path: str
    fleet_poll_interval: int = 5
    fleet_sidecar_port: int = 8001
    fleet_max_concurrent_tasks: int = 1

    model_config = {"env_prefix": ""}
