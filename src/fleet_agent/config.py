"""Sidecar configuration loaded from environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class SidecarConfig(BaseSettings):
    """Configuration for the fleet agent sidecar.

    All values are read from environment variables with the exact names shown
    (no prefix stripping).  ``fleet_api_url``, ``fleet_agent_id``,
    ``fleet_agent_private_key_path``, and ``fleet_executor_command`` are
    required; the rest have sensible defaults.
    """

    fleet_api_url: str
    fleet_agent_id: str
    fleet_agent_private_key_path: str
    fleet_poll_interval: int = Field(
        default=5, description="Poll interval in seconds"
    )
    fleet_sidecar_port: int = Field(
        default=8001, description="Local health endpoint port"
    )
    fleet_max_concurrent_tasks: int = Field(
        default=1, description="Maximum concurrent task executions"
    )
    fleet_executor_command: str = Field(
        description="Shell command the executor runs for each task via subprocess"
    )
    fleet_heartbeat_interval: int = Field(
        default=30, description="Heartbeat interval in seconds"
    )
    fleet_signal_poll_interval: int = Field(
        default=2,
        description=(
            "Signal poll interval in seconds.  Shorter than fleet_poll_interval "
            "because signals (pause/resume/cancel/redirect/context) are latency-"
            "sensitive control plane operations — the principal expects sub-second "
            "to low-single-digit second response times."
        ),
    )

    model_config = {"env_prefix": ""}
