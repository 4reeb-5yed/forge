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



class TestGetComponentHealth:
    """Tests for ConfigService.get_component_health."""

    async def test_get_component_health_all_healthy(
        self, configured_service: ConfigService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When all probes succeed, all components report healthy."""
        from unittest.mock import AsyncMock, patch, MagicMock
        import subprocess

        # Mock openrouter key test to succeed
        mock_or_result = MagicMock()
        mock_or_result.success = True
        mock_or_result.latency_ms = 150.0
        mock_or_result.error = ""

        # Mock github key test to succeed
        mock_gh_result = MagicMock()
        mock_gh_result.success = True
        mock_gh_result.latency_ms = 120.0
        mock_gh_result.error = ""

        with patch.object(
            configured_service, "_test_openrouter_key", new_callable=AsyncMock, return_value=mock_or_result
        ), patch.object(
            configured_service, "_test_github_key", new_callable=AsyncMock, return_value=mock_gh_result
        ), patch("shutil.which", return_value="/usr/bin/docker"), patch(
            "subprocess.run"
        ) as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/forge")

            health = await configured_service.get_component_health()

        assert health["openrouter"]["status"] == "healthy"
        assert health["github"]["status"] == "healthy"
        assert health["docker"]["status"] == "healthy"
        assert health["database"]["status"] == "healthy"
        assert health["event_bus"]["status"] == "healthy"

    async def test_get_component_health_missing_keys(
        self, service: ConfigService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When API keys are not set, openrouter and github report unhealthy."""
        from unittest.mock import patch

        with patch("shutil.which", return_value="/usr/bin/docker"), patch(
            "subprocess.run"
        ) as mock_run:
            import subprocess

            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/forge")

            health = await service.get_component_health()

        assert health["openrouter"]["status"] == "unhealthy"
        assert health["openrouter"]["message"] == "API key not configured"
        assert health["github"]["status"] == "unhealthy"
        assert health["github"]["message"] == "API key not configured"

    async def test_get_component_health_docker_not_found(
        self, service: ConfigService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When docker is not in PATH, docker reports unhealthy."""
        from unittest.mock import patch

        with patch("shutil.which", return_value=None):
            monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/forge")
            health = await service.get_component_health()

        assert health["docker"]["status"] == "unhealthy"
        assert "not found" in health["docker"]["message"].lower()

    async def test_get_component_health_database_url_not_set(
        self, service: ConfigService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When DATABASE_URL is not set, database reports unhealthy."""
        from unittest.mock import patch

        monkeypatch.delenv("DATABASE_URL", raising=False)

        with patch("shutil.which", return_value=None):
            health = await service.get_component_health()

        assert health["database"]["status"] == "unhealthy"
        assert "DATABASE_URL" in health["database"]["message"]

    async def test_get_component_health_event_bus_always_healthy(
        self, service: ConfigService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """event_bus is always healthy."""
        from unittest.mock import patch

        with patch("shutil.which", return_value=None):
            monkeypatch.delenv("DATABASE_URL", raising=False)
            health = await service.get_component_health()

        assert health["event_bus"]["status"] == "healthy"


class TestGetModels:
    """Tests for ConfigService.get_models."""

    async def test_get_models_raises_without_key(self, service: ConfigService) -> None:
        """Raises ValueError when OpenRouter API key is not configured."""
        with pytest.raises(ValueError, match="OpenRouter API key not configured"):
            await service.get_models()

    async def test_get_models_fetches_and_caches(
        self, configured_service: ConfigService
    ) -> None:
        """Fetches models from OpenRouter and caches the result."""
        from unittest.mock import AsyncMock, patch, MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"id": "anthropic/claude-sonnet-4-20250514", "name": "Claude Sonnet"},
                {"id": "openai/gpt-4o", "name": "GPT-4o"},
            ]
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            models = await configured_service.get_models()

        assert len(models) == 2
        assert models[0] == {"id": "anthropic/claude-sonnet-4-20250514", "name": "Claude Sonnet"}
        assert models[1] == {"id": "openai/gpt-4o", "name": "GPT-4o"}

    async def test_get_models_caches_results(
        self, configured_service: ConfigService
    ) -> None:
        """Second call returns cached results without making a second HTTP request."""
        from unittest.mock import AsyncMock, patch, MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"id": "anthropic/claude-sonnet-4-20250514", "name": "Claude Sonnet"},
            ]
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            # First call — fetches from API
            models1 = await configured_service.get_models()
            # Second call — should use cache
            models2 = await configured_service.get_models()

        # httpx.AsyncClient should only have been instantiated once
        assert mock_cls.call_count == 1
        assert models1 == models2

    async def test_get_models_refreshes_after_ttl(
        self, configured_service: ConfigService
    ) -> None:
        """Cache is refreshed after TTL expires."""
        import time
        from unittest.mock import AsyncMock, patch, MagicMock

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"id": "openai/gpt-4o", "name": "GPT-4o"},
            ]
        }

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # Set TTL very short so we can expire it
        configured_service._state.model_cache_ttl_seconds = 1

        with patch("httpx.AsyncClient", return_value=mock_client) as mock_cls:
            # First call fetches
            await configured_service.get_models()
            assert mock_cls.call_count == 1

            # Expire the cache manually
            configured_service._model_cache_time = time.time() - 10

            # Second call should re-fetch
            await configured_service.get_models()
            assert mock_cls.call_count == 2
