"""Unit tests for ConfigService — get_config, update_config, and _redact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.runtime.config import (
    ConfigService,
    ConfigState,
    ConfigValidationError,
    SandboxMode,
)
from app.runtime.events.models import Event


async def _noop_emitter(event: Event) -> Event:
    """No-op event emitter for tests that don't need event handling."""
    return event


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Return a path inside a temp dir for the config file."""
    return tmp_path / "forge-config.json"


@pytest.fixture
def service(config_path: Path) -> ConfigService:
    """Return a ConfigService with default (unconfigured) state."""
    return ConfigService(config_path=config_path, event_emitter=_noop_emitter)


@pytest.fixture
def configured_service(config_path: Path) -> ConfigService:
    """Return a ConfigService pre-loaded with a valid configuration."""
    svc = ConfigService(config_path=config_path, event_emitter=_noop_emitter)
    svc._state = ConfigState(
        openrouter_api_key="sk-or-v1-abcdefghij1234",
        github_token="ghp_0123456789abcdef",
        selected_model="anthropic/claude-sonnet-4-20250514",
        sandbox_mode=SandboxMode.AUTO,
        model_cache_ttl_seconds=3600,
    )
    return svc


class TestRedact:
    """Tests for ConfigService._redact static method."""

    def test_empty_string_returns_empty(self) -> None:
        assert ConfigService._redact("") == ""

    def test_short_value_fully_masked(self) -> None:
        assert ConfigService._redact("abcd") == "****"
        assert ConfigService._redact("abc") == "****"
        assert ConfigService._redact("a") == "****"

    def test_sk_or_prefix_preserved(self) -> None:
        # "sk-or-" prefix is matched (longer prefix wins)
        result = ConfigService._redact("sk-or-v1-abc123xyz")
        assert result == "sk-or-****3xyz"
        assert result.startswith("sk-or-")
        assert result.endswith("3xyz")

    def test_sk_prefix_preserved(self) -> None:
        # "sk-" prefix matched when "sk-or-" doesn't apply
        result = ConfigService._redact("sk-live-abcdef1234")
        assert result == "sk-****1234"
        assert result.startswith("sk-")
        assert result.endswith("1234")

    def test_ghp_prefix_preserved(self) -> None:
        result = ConfigService._redact("ghp_abcdefghij1234")
        assert result == "ghp_****1234"
        assert result.startswith("ghp_")
        assert result.endswith("1234")

    def test_github_pat_prefix_preserved(self) -> None:
        result = ConfigService._redact("github_pat_abcdefghij1234")
        assert result == "github_pat_****1234"

    def test_no_prefix_shows_last_4(self) -> None:
        result = ConfigService._redact("some-random-secret-value")
        assert result == "****alue"
        assert result.endswith("alue")

    def test_exactly_5_chars(self) -> None:
        # Longer than 4, no known prefix
        result = ConfigService._redact("12345")
        assert result == "****2345"


class TestGetConfig:
    """Tests for ConfigService.get_config."""

    async def test_unconfigured_returns_configured_false(self, service: ConfigService) -> None:
        result = await service.get_config()
        assert result["configured"] is False

    async def test_configured_returns_configured_true(
        self, configured_service: ConfigService
    ) -> None:
        result = await configured_service.get_config()
        assert result["configured"] is True

    async def test_secrets_are_redacted(self, configured_service: ConfigService) -> None:
        result = await configured_service.get_config()
        # API key should be redacted, not the full value
        assert result["openrouter_api_key"] != "sk-or-v1-abcdefghij1234"
        assert result["openrouter_api_key"].endswith("1234")
        assert "****" in result["openrouter_api_key"]
        # GitHub token should be redacted
        assert result["github_token"] != "ghp_0123456789abcdef"
        assert "****" in result["github_token"]

    async def test_non_secret_fields_unmodified(
        self, configured_service: ConfigService
    ) -> None:
        result = await configured_service.get_config()
        assert result["selected_model"] == "anthropic/claude-sonnet-4-20250514"
        assert result["sandbox_mode"] == "auto"
        assert result["model_cache_ttl_seconds"] == 3600

    async def test_empty_secrets_return_empty_string(self, service: ConfigService) -> None:
        result = await service.get_config()
        assert result["openrouter_api_key"] == ""
        assert result["github_token"] == ""


class TestUpdateConfig:
    """Tests for ConfigService.update_config."""

    async def test_update_single_field(self, service: ConfigService) -> None:
        result = await service.update_config({"selected_model": "openai/gpt-4o"})
        assert result["selected_model"] == "openai/gpt-4o"

    async def test_update_api_key_persists_and_redacts(
        self, service: ConfigService, config_path: Path
    ) -> None:
        result = await service.update_config(
            {"openrouter_api_key": "sk-or-v1-newkey12345678"}
        )
        # Response is redacted
        assert "****" in result["openrouter_api_key"]
        assert result["openrouter_api_key"].endswith("5678")
        # But persisted file has full value
        data = json.loads(config_path.read_text())
        assert data["openrouter_api_key"] == "sk-or-v1-newkey12345678"

    async def test_update_sandbox_mode_valid(self, service: ConfigService) -> None:
        result = await service.update_config({"sandbox_mode": "always"})
        assert result["sandbox_mode"] == "always"
        assert service.state.sandbox_mode == SandboxMode.ALWAYS

    async def test_update_sandbox_mode_invalid_raises(self, service: ConfigService) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            await service.update_config({"sandbox_mode": "invalid_mode"})
        assert "sandbox_mode" in exc_info.value.errors
        assert "invalid_mode" in exc_info.value.errors["sandbox_mode"]

    async def test_update_model_cache_ttl_valid(self, service: ConfigService) -> None:
        result = await service.update_config({"model_cache_ttl_seconds": 7200})
        assert result["model_cache_ttl_seconds"] == 7200

    async def test_update_model_cache_ttl_zero_raises(self, service: ConfigService) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            await service.update_config({"model_cache_ttl_seconds": 0})
        assert "model_cache_ttl_seconds" in exc_info.value.errors

    async def test_update_model_cache_ttl_negative_raises(self, service: ConfigService) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            await service.update_config({"model_cache_ttl_seconds": -100})
        assert "model_cache_ttl_seconds" in exc_info.value.errors

    async def test_update_model_cache_ttl_non_int_raises(self, service: ConfigService) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            await service.update_config({"model_cache_ttl_seconds": 3.14})
        assert "model_cache_ttl_seconds" in exc_info.value.errors

    async def test_multiple_validation_errors(self, service: ConfigService) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            await service.update_config(
                {"sandbox_mode": "bad", "model_cache_ttl_seconds": -1}
            )
        assert "sandbox_mode" in exc_info.value.errors
        assert "model_cache_ttl_seconds" in exc_info.value.errors

    async def test_invalid_payload_does_not_modify_state(
        self, service: ConfigService
    ) -> None:
        original_mode = service.state.sandbox_mode
        with pytest.raises(ConfigValidationError):
            await service.update_config({"sandbox_mode": "bad"})
        assert service.state.sandbox_mode == original_mode

    async def test_invalid_payload_does_not_persist(
        self, service: ConfigService, config_path: Path
    ) -> None:
        with pytest.raises(ConfigValidationError):
            await service.update_config({"sandbox_mode": "invalid"})
        # File should not exist since no valid save was done
        assert not config_path.exists()

    async def test_update_returns_full_config(self, service: ConfigService) -> None:
        result = await service.update_config({"selected_model": "test/model"})
        # Should include all config fields
        assert "configured" in result
        assert "openrouter_api_key" in result
        assert "github_token" in result
        assert "selected_model" in result
        assert "sandbox_mode" in result
        assert "model_cache_ttl_seconds" in result

    async def test_partial_update_preserves_other_fields(
        self, configured_service: ConfigService
    ) -> None:
        await configured_service.update_config({"sandbox_mode": "never"})
        assert configured_service.state.sandbox_mode == SandboxMode.NEVER
        # Other fields remain unchanged
        assert configured_service.state.selected_model == "anthropic/claude-sonnet-4-20250514"
        assert configured_service.state.model_cache_ttl_seconds == 3600


class TestConfigValidationError:
    """Tests for ConfigValidationError exception."""

    def test_stores_errors_dict(self) -> None:
        err = ConfigValidationError({"field1": "bad", "field2": "also bad"})
        assert err.errors == {"field1": "bad", "field2": "also bad"}

    def test_has_meaningful_str(self) -> None:
        err = ConfigValidationError({"sandbox_mode": "invalid"})
        assert "sandbox_mode" in str(err)
