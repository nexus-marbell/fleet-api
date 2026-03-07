"""Tests for fleet_agent.config."""

from __future__ import annotations

import pytest

from fleet_agent.config import SidecarConfig


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set all required env vars for SidecarConfig."""
    monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
    monkeypatch.setenv("FLEET_AGENT_ID", "agent-1")
    monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
    monkeypatch.setenv("FLEET_EXECUTOR_COMMAND", "fleet-handler")


class TestSidecarConfig:
    """Configuration loading from environment variables."""

    def test_loads_required_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Required fields are read from environment variables."""
        _set_required_env(monkeypatch)

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_api_url == "https://fleet.example.com"
        assert config.fleet_agent_id == "agent-1"
        assert config.fleet_agent_private_key_path == "/keys/agent.pem"
        assert config.fleet_executor_command == "fleet-handler"

    def test_defaults_for_optional_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Optional fields use defaults when not set."""
        _set_required_env(monkeypatch)

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_poll_interval == 5
        assert config.fleet_sidecar_port == 8001
        assert config.fleet_max_concurrent_tasks == 1
        assert config.fleet_heartbeat_interval == 30

    def test_overrides_optional_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Optional fields can be overridden via env vars."""
        _set_required_env(monkeypatch)
        monkeypatch.setenv("FLEET_POLL_INTERVAL", "10")
        monkeypatch.setenv("FLEET_SIDECAR_PORT", "9001")
        monkeypatch.setenv("FLEET_MAX_CONCURRENT_TASKS", "4")
        monkeypatch.setenv("FLEET_HEARTBEAT_INTERVAL", "15")

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_poll_interval == 10
        assert config.fleet_sidecar_port == 9001
        assert config.fleet_max_concurrent_tasks == 4
        assert config.fleet_heartbeat_interval == 15


class TestExecutorCommandRequired:
    """FLEET_EXECUTOR_COMMAND is required -- missing raises ValidationError."""

    def test_missing_executor_command_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SidecarConfig fails to instantiate without FLEET_EXECUTOR_COMMAND."""
        monkeypatch.setenv("FLEET_API_URL", "https://fleet.example.com")
        monkeypatch.setenv("FLEET_AGENT_ID", "agent-1")
        monkeypatch.setenv("FLEET_AGENT_PRIVATE_KEY_PATH", "/keys/agent.pem")
        # Deliberately NOT setting FLEET_EXECUTOR_COMMAND.
        monkeypatch.delenv("FLEET_EXECUTOR_COMMAND", raising=False)

        with pytest.raises(Exception):
            SidecarConfig()  # type: ignore[call-arg]

    def test_executor_command_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FLEET_EXECUTOR_COMMAND is read from environment."""
        _set_required_env(monkeypatch)
        monkeypatch.setenv("FLEET_EXECUTOR_COMMAND", "my-custom-handler")

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_executor_command == "my-custom-handler"


class TestHeartbeatIntervalDefault:
    """FLEET_HEARTBEAT_INTERVAL defaults to 30."""

    def test_default_heartbeat_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default heartbeat interval is 30 seconds."""
        _set_required_env(monkeypatch)

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_heartbeat_interval == 30

    def test_custom_heartbeat_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Heartbeat interval can be overridden via env var."""
        _set_required_env(monkeypatch)
        monkeypatch.setenv("FLEET_HEARTBEAT_INTERVAL", "60")

        config = SidecarConfig()  # type: ignore[call-arg]

        assert config.fleet_heartbeat_interval == 60
