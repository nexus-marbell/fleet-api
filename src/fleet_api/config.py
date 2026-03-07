"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Fleet API configuration loaded from environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://fleet:fleet@localhost:5432/fleet_api"

    # Server
    fleet_api_host: str = "0.0.0.0"
    fleet_api_port: int = 8000

    # API identity
    api_version: str = "1.0.0"
    base_url: str = "http://localhost:8000"

    # Rate limiting
    rate_limit_rpm: int = 120
    rate_limit_burst: int = 20

    # Limits (RFC 1 defaults)
    fleet_task_retention_days: int = 30
    fleet_retask_max_depth: int = 10
    fleet_delegation_max_depth: int = 4
    fleet_heartbeat_timeout_seconds: int = 90
    fleet_heartbeat_sweep_interval: int = 30
    fleet_pause_ttl_seconds: int = 3600

    # SSE streaming
    fleet_sse_heartbeat_interval: int = 15

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
