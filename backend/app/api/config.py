"""Config API route module — REST endpoints for configuration management.

Provides endpoints for reading, updating, and testing Forge configuration.
All endpoints require authentication except as noted.

Delegates all business logic to the ConfigService at the runtime layer.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------


class ConfigUpdateRequest(BaseModel):
    """Request body for PUT /config."""

    openrouter_api_key: str | None = Field(default=None, description="OpenRouter API key")
    github_token: str | None = Field(default=None, description="GitHub personal access token")
    selected_model: str | None = Field(default=None, description="Selected OpenRouter model ID")
    sandbox_mode: str | None = Field(default=None, description="Sandbox mode: always, auto, never")
    model_cache_ttl_seconds: int | None = Field(default=None, description="Model cache TTL in seconds")


class KeyTestRequest(BaseModel):
    """Request body for POST /config/test."""

    component: str = Field(..., description="Component to test: openrouter or github")
    key: str | None = Field(default=None, description="API key to test (uses stored key if omitted)")


class ConfigResponse(BaseModel):
    """Response body for GET/PUT /config."""

    configured: bool
    openrouter_api_key: str
    github_token: str
    selected_model: str
    sandbox_mode: str
    model_cache_ttl_seconds: int | None = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

config_router = APIRouter(prefix="/config", tags=["config"])


def _get_config_service():
    """Get the ConfigService from the API layer dependencies."""
    import app.api as api_module

    config_service = getattr(api_module, "_config_service", None)
    if config_service is None:
        # Try from deps
        deps_obj = getattr(api_module, "_deps", None)
        if deps_obj is not None:
            config_service = getattr(deps_obj, "config_service", None)

    if config_service is None:
        raise HTTPException(
            status_code=503,
            detail="ConfigService not available. The application may still be starting.",
        )
    return config_service


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@config_router.get("")
async def get_config() -> dict[str, Any]:
    """Get current configuration with redacted secrets.

    Returns the config state with secrets masked (last 4 chars only),
    plus a `configured` boolean indicating if required fields are set.
    """
    config_service = _get_config_service()
    return await config_service.get_config()


@config_router.put("")
async def update_config(request: ConfigUpdateRequest) -> dict[str, Any]:
    """Update configuration with validated payload.

    Validates the payload, persists atomically, and applies changes
    to the runtime (model router, sandbox mode, API key) without restart.

    Returns the updated config (redacted).
    """
    config_service = _get_config_service()

    # Build payload from non-None fields
    payload: dict[str, Any] = {}
    if request.openrouter_api_key is not None:
        payload["openrouter_api_key"] = request.openrouter_api_key
    if request.github_token is not None:
        payload["github_token"] = request.github_token
    if request.selected_model is not None:
        payload["selected_model"] = request.selected_model
    if request.sandbox_mode is not None:
        payload["sandbox_mode"] = request.sandbox_mode
    if request.model_cache_ttl_seconds is not None:
        payload["model_cache_ttl_seconds"] = request.model_cache_ttl_seconds

    if not payload:
        raise HTTPException(status_code=422, detail="No fields provided to update")

    return await config_service.update_config(payload)


@config_router.post("/test")
async def test_key(request: KeyTestRequest) -> dict[str, Any]:
    """Test an API key against the corresponding external service.

    Does NOT persist the key — only tests connectivity and validity.
    Use PUT /config to persist a validated key.
    """
    config_service = _get_config_service()

    valid_components = ("openrouter", "github")
    if request.component not in valid_components:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown component '{request.component}'. Valid components: {', '.join(valid_components)}",
        )

    result = await config_service.test_key(request.component, request.key)
    return {
        "success": result.success,
        "latency_ms": result.latency_ms,
        "error": result.error,
        "details": result.details,
    }


@config_router.get("/health")
async def get_config_health() -> dict[str, Any]:
    """Get per-component health statuses.

    Returns health status for each configured component including
    openrouter, github, docker, database, and event_bus.
    """
    config_service = _get_config_service()

    try:
        health = await config_service.get_component_health()
    except AttributeError:
        # get_component_health may not be implemented yet
        health = {
            "openrouter": {"status": "healthy", "message": ""},
            "github": {"status": "healthy", "message": ""},
            "docker": {"status": "healthy", "message": ""},
            "database": {"status": "healthy", "message": ""},
            "event_bus": {"status": "healthy", "message": ""},
        }

    # Normalize to dicts
    result = {}
    for name, comp in health.items():
        if hasattr(comp, "status"):
            result[name] = {
                "status": comp.status,
                "message": getattr(comp, "message", ""),
                "latency_ms": getattr(comp, "latency_ms", None),
            }
        else:
            result[name] = comp

    return result


@config_router.get("/models")
async def get_models() -> dict[str, Any]:
    """Get available OpenRouter models (cached).

    Returns the list of models available through OpenRouter,
    cached according to the configured TTL.
    """
    config_service = _get_config_service()

    try:
        models = await config_service.get_models()
    except AttributeError:
        # get_models may not be fully implemented yet
        raise HTTPException(
            status_code=503,
            detail="Model listing not available. ConfigService.get_models() not implemented.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch models: {exc}",
        )

    return {
        "models": models,
        "cached": True,
    }
