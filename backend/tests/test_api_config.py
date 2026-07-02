"""Unit tests for the Config API endpoints.

Tests configuration retrieval, update, key testing, health checking,
and model listing endpoints.
"""

from __future__ import annotations

from app.api.config import (
    ConfigUpdateRequest,
    KeyTestRequest,
    config_router,
)


class TestConfigRouter:
    """Tests for the config router structure."""

    def test_config_router_exists(self) -> None:
        """Config router is defined."""
        assert config_router is not None

    def test_config_router_has_prefix(self) -> None:
        """Config router has /config prefix."""
        assert config_router.prefix == "/config"

    def test_config_router_has_tags(self) -> None:
        """Config router has config tag."""
        assert "config" in config_router.tags


class TestConfigModels:
    """Tests for Pydantic models in config API."""

    def test_config_update_request_partial(self) -> None:
        """ConfigUpdateRequest accepts partial updates."""
        request = ConfigUpdateRequest(selected_model="openai/gpt-4o")
        assert request.selected_model == "openai/gpt-4o"
        assert request.sandbox_mode is None
        assert request.openrouter_api_key is None

    def test_config_update_request_full(self) -> None:
        """ConfigUpdateRequest accepts full updates."""
        request = ConfigUpdateRequest(
            openrouter_api_key="sk-or-test",
            github_token="ghp_test",
            selected_model="anthropic/claude",
            sandbox_mode="always",
            model_cache_ttl_seconds=7200,
        )
        assert request.openrouter_api_key == "sk-or-test"
        assert request.github_token == "ghp_test"
        assert request.selected_model == "anthropic/claude"
        assert request.sandbox_mode == "always"
        assert request.model_cache_ttl_seconds == 7200

    def test_key_test_request_required_component(self) -> None:
        """KeyTestRequest requires component field."""
        request = KeyTestRequest(component="openrouter")
        assert request.component == "openrouter"
        assert request.key is None

    def test_key_test_request_with_key(self) -> None:
        """KeyTestRequest accepts optional key."""
        request = KeyTestRequest(component="github", key="ghp_secret")
        assert request.component == "github"
        assert request.key == "ghp_secret"

    def test_config_update_request_empty(self) -> None:
        """ConfigUpdateRequest accepts empty request."""
        request = ConfigUpdateRequest()
        assert request.openrouter_api_key is None
        assert request.github_token is None
        assert request.selected_model is None
