"""Tests for fleet_agent.config."""

from __future__ import annotations

import pytest

from fleet_agent.config import SidecarConfig


class TestSidecarConfig:
    """Configuration loading from environment variables."""

    def test_loads_required_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Required fields are read from environment variables."""
        monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
        monkeypatch.setenv("FLEET_AGENT_ID", "agent-1")
        monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_api_url == "https://fleet.example.com"
        assert config.fleet_agent_id == "agent-1"
        assert config.fleet_agent_private_key_path == "/keys/agent.pem"

    def test_defaults_for_optional_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Optional fields use defaults when not set."""
        monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
        monkeypatch.setenv("FLEET_AGENT_ID", "agent-1")
        monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_poll_interval == 5
        assert config.fleet_sidecar_port == 8001
        assert config.fleet_max_concurrent_tasks == 1

    def test_overrides_optional_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Optional fields can be overridden via env vars."""
        monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
        monkeypatch.setenv("FLEET_AGENT_ID", "agent-1")
        monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
        monkeypatch.setenv("FLEET_POLL_INTERVAL", "10")
        monkeypatch.setenv("FLEET_SIDECAR_PORT", "9001")
        monkeypatch.setenv("FLEET_MAX_CONCURRENT_TASKS", "4")

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_poll_interval == 10
        assert config.fleet_sidecar_port == 9001
        assert config.fleet_max_concurrent_tasks == 4
