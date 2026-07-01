"""ConfigService — runtime configuration manager for Forge.

Loads, validates, persists, and serves Forge configuration from a JSON file
on a mounted volume. Single source of truth for all config state.

Responsibilities:
- Load config from JSON file on startup
- Persist config atomically (write-tmp-rename) with restrictive permissions
- Emit CONFIG_ERROR events on failures (corrupt file, missing deps)
- Apply config changes to runtime without restart (hot-reload)
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType

if TYPE_CHECKING:
    from app.workflow.deps import RuntimeDeps

logger = logging.getLogger(__name__)

# Type alias for the event emitter callable (matches EventBus.publish signature)
EventEmitter = Callable[[Event], Awaitable[Event]]

# Fields that contain secrets and must be redacted in API responses
_SECRET_FIELDS = frozenset({"openrouter_api_key", "github_token"})


class ConfigValidationError(Exception):
    """Raised when config update payload fails validation.

    Attributes:
        errors: Dict mapping field names to human-readable error messages.
    """

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__(f"Config validation failed: {errors}")


class SandboxMode(str, Enum):
    """Sandbox execution modes for AI code execution."""

    ALWAYS = "always"
    AUTO = "auto"
    NEVER = "never"


@dataclass
class ConfigState:
    """The full configuration state persisted to disk.

    Fields:
        openrouter_api_key: API key for the OpenRouter service.
        github_token: Personal access token for GitHub integration.
        selected_model: The OpenRouter model identifier to use.
        sandbox_mode: Docker sandbox execution policy.
        model_cache_ttl_seconds: TTL for cached model list (default 1 hour).
    """

    openrouter_api_key: str = ""
    github_token: str = ""
    selected_model: str = ""
    sandbox_mode: SandboxMode = SandboxMode.AUTO
    model_cache_ttl_seconds: int = 3600

    @property
    def configured(self) -> bool:
        """True when all required fields are set."""
        return bool(self.openrouter_api_key and self.selected_model)


@dataclass
class KeyTestResult:
    """Result of an API key verification probe.

    Fields:
        success: Whether the key was accepted by the external service.
        latency_ms: Round-trip time of the verification request in milliseconds.
        error: Human-readable error message on failure (empty on success).
        details: Additional response details (e.g. models_available, username).
    """

    success: bool
    latency_ms: float
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class ConfigService:
    """Runtime configuration manager.

    Manages loading and persisting configuration to a JSON file with
    atomic writes and restrictive permissions. Emits error events when
    configuration issues are detected.
    """

    def __init__(
        self,
        config_path: Path,
        event_emitter: EventEmitter,
    ) -> None:
        """Initialize the ConfigService.

        Args:
            config_path: Path to the JSON configuration file.
            event_emitter: Async callable to publish events (EventBus.publish).
        """
        self._config_path = config_path
        self._event_emitter = event_emitter
        self._state = ConfigState()
        self._deps: RuntimeDeps | None = None
        self._last_openrouter_key: str = ""
        self._model_cache: list[dict[str, str]] | None = None
        self._model_cache_time: float = 0.0

    @property
    def state(self) -> ConfigState:
        """Current configuration state."""
        return self._state

    def set_deps(self, deps: RuntimeDeps) -> None:
        """Store a reference to RuntimeDeps for hot-reload of configuration.

        Called during bootstrap wiring to connect the ConfigService to the
        runtime components it needs to update when config changes.

        Args:
            deps: The RuntimeDeps container holding model_router, coding_tool, etc.
        """
        self._deps = deps
        # Track the current key so we can detect changes later
        self._last_openrouter_key = self._state.openrouter_api_key

    def apply_to_runtime(self) -> None:
        """Apply current config state to runtime components without restart.

        Updates:
        1. Model router chain_config — sets selected_model across all roles
        2. Sandbox mode — updates the FORGE_USE_SANDBOX env var for next execution
        3. OpenRouter call adapter — recreates if the API key changed

        If deps have not been set (via set_deps), this method is a no-op.
        This allows testing ConfigService in isolation without wiring the
        full runtime.
        """
        if self._deps is None:
            logger.debug("apply_to_runtime skipped: deps not set")
            return

        self._apply_model_to_router()
        self._apply_sandbox_mode()
        self._apply_openrouter_key()

    def _apply_model_to_router(self) -> None:
        """Update model router chain_config to use the currently selected model."""
        if not self._state.selected_model or self._deps is None:
            return

        from app.runtime.models import Role
        from app.runtime.router import ChainEntry, RoleChainConfig

        new_chains: dict[Role, list[ChainEntry]] = {}
        for role in Role:
            new_chains[role] = [
                ChainEntry(provider="openrouter", model=self._state.selected_model)
            ]

        self._deps.model_router._chain_config = RoleChainConfig(chains=new_chains)
        logger.info(
            "Model router updated: all roles now use model '%s'",
            self._state.selected_model,
        )

    def _apply_sandbox_mode(self) -> None:
        """Update the FORGE_USE_SANDBOX env var based on sandbox_mode config.

        The coding tool reads this env var on each execution to decide
        whether to use Docker sandboxing. Updating the env var is sufficient
        for the next execution — no tool recreation needed.
        """
        os.environ["FORGE_USE_SANDBOX"] = self._state.sandbox_mode.value
        logger.info(
            "Sandbox mode updated: FORGE_USE_SANDBOX='%s'",
            self._state.sandbox_mode.value,
        )

    def _apply_openrouter_key(self) -> None:
        """Recreate the OpenRouter call adapter if the API key changed.

        Detects key changes by comparing against the last known key.
        When the key changes, creates a new OpenRouterProvider with the
        new key and replaces the call adapter on the model router.
        """
        if self._deps is None:
            return

        current_key = self._state.openrouter_api_key
        if current_key == self._last_openrouter_key:
            return

        if not current_key:
            logger.warning("OpenRouter API key cleared — call adapter unchanged")
            self._last_openrouter_key = current_key
            return

        from app.adapters.openrouter import OpenRouterProvider

        provider = OpenRouterProvider(api_key=current_key)
        self._deps.model_router._call_adapter = provider.as_call_adapter()
        self._last_openrouter_key = current_key

        # Also update the env var so the coding tool (Aider) picks up the new key
        os.environ["OPENROUTER_API_KEY"] = current_key

        logger.info("OpenRouter call adapter recreated with new API key")

    async def load(self) -> ConfigState:
        """Load configuration from the JSON file.

        Handles three cases:
        1. File exists and is valid JSON → deserialize to ConfigState
        2. File does not exist → use env vars as fallback
        3. File exists but contains corrupt JSON → log error, use defaults,
           emit CONFIG_ERROR event

        Returns:
            The loaded (or default) ConfigState.
        """
        if not self._config_path.exists():
            logger.info(
                "Config file not found at %s, checking environment variables", self._config_path
            )
            # Use environment variables as fallback
            state = ConfigState()
            state.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
            state.github_token = os.environ.get("GITHUB_TOKEN", "")
            state.selected_model = os.environ.get("FORGE_MODEL", "anthropic/claude-3-haiku")
            sandbox_env = os.environ.get("FORGE_USE_SANDBOX", "auto")
            try:
                state.sandbox_mode = SandboxMode(sandbox_env)
            except ValueError:
                state.sandbox_mode = SandboxMode.AUTO
            self._state = state
            self._last_openrouter_key = self._state.openrouter_api_key
            return self._state

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._state = self._deserialize(data)
            self._last_openrouter_key = self._state.openrouter_api_key
            logger.info("Configuration loaded from %s", self._config_path)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "Corrupt config file at %s: %s. Using environment variables.",
                self._config_path,
                exc,
            )
            # Fall back to environment variables
            state = ConfigState()
            state.openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")
            state.github_token = os.environ.get("GITHUB_TOKEN", "")
            state.selected_model = os.environ.get("FORGE_MODEL", "anthropic/claude-3-haiku")
            self._state = state
            await self._emit_config_error(
                code="CONFIG_FILE_CORRUPT",
                message=f"Configuration file is corrupt: {exc}. Using environment variables.",
                component="config_service",
                recoverable=True,
                suggestion="Reconfigure Forge via the Setup Wizard to overwrite the corrupt file.",
            )

        return self._state

    @staticmethod
    def _redact(value: str) -> str:
        """Mask a secret string, showing only the last 4 characters.

        Examples:
            "sk-or-v1-abc123xyz" → "sk-****xyz"  (prefix preserved if starts with known prefix)
            "ghp_abcdefghij1234" → "ghp_****1234"
            Short values (≤4 chars) are fully masked as "****".
        """
        if not value:
            return ""
        if len(value) <= 4:
            return "****"
        last4 = value[-4:]
        # Preserve common key prefixes for UX clarity
        for prefix in ("sk-or-", "sk-", "ghp_", "github_pat_"):
            if value.startswith(prefix):
                return f"{prefix}****{last4}"
        return f"****{last4}"

    async def get_config(self) -> dict[str, Any]:
        """Return the current config state with secrets redacted.

        Returns a dict suitable for JSON serialization containing all
        config fields, with secret fields masked via _redact(), plus a
        top-level `configured` boolean.
        """
        data: dict[str, Any] = {
            "openrouter_api_key": self._redact(self._state.openrouter_api_key),
            "github_token": self._redact(self._state.github_token),
            "selected_model": self._state.selected_model,
            "sandbox_mode": self._state.sandbox_mode.value,
            "model_cache_ttl_seconds": self._state.model_cache_ttl_seconds,
            "configured": self._state.configured,
        }
        return data

    async def update_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and apply a partial config update.

        Args:
            payload: Dict with any subset of config fields to update.

        Returns:
            The full redacted config dict (same shape as get_config()).

        Raises:
            ConfigValidationError: If any field has an invalid value.
        """
        errors: dict[str, str] = {}

        # Validate sandbox_mode if provided
        if "sandbox_mode" in payload:
            raw_mode = payload["sandbox_mode"]
            try:
                SandboxMode(raw_mode)
            except ValueError:
                valid = ", ".join(m.value for m in SandboxMode)
                errors["sandbox_mode"] = (
                    f"Invalid value '{raw_mode}'. Must be one of: {valid}"
                )

        # Validate model_cache_ttl_seconds if provided
        if "model_cache_ttl_seconds" in payload:
            ttl = payload["model_cache_ttl_seconds"]
            if not isinstance(ttl, int) or ttl <= 0:
                errors["model_cache_ttl_seconds"] = (
                    "Must be a positive integer"
                )

        if errors:
            raise ConfigValidationError(errors)

        # Apply valid fields to state
        if "openrouter_api_key" in payload:
            self._state.openrouter_api_key = payload["openrouter_api_key"]
        if "github_token" in payload:
            self._state.github_token = payload["github_token"]
        if "selected_model" in payload:
            self._state.selected_model = payload["selected_model"]
        if "sandbox_mode" in payload:
            self._state.sandbox_mode = SandboxMode(payload["sandbox_mode"])
        if "model_cache_ttl_seconds" in payload:
            self._state.model_cache_ttl_seconds = payload["model_cache_ttl_seconds"]

        # Persist atomically
        await self._save()

        # Apply changes to runtime (model router, sandbox mode, API key)
        self.apply_to_runtime()

        return await self.get_config()

    async def test_key(self, component: str, key: str | None = None) -> KeyTestResult:
        """Test an API key against the corresponding external service.

        Args:
            component: Service name — "openrouter" or "github".
            key: The API key to test. If None, uses the stored key from state.

        Returns:
            KeyTestResult with success status, latency, and any details.

        Raises:
            ValueError: If component is not a recognized service name.
        """
        if component == "openrouter":
            return await self._test_openrouter_key(key)
        elif component == "github":
            return await self._test_github_key(key)
        else:
            raise ValueError(
                f"Unknown component '{component}'. Valid components: openrouter, github"
            )

    async def get_component_health(self) -> dict[str, dict[str, Any]]:
        """Probe all components and return per-component health status.

        Components probed:
        - openrouter: test API key validity via GET /models
        - github: test token validity via GET /user
        - docker: check if Docker CLI exists and sandbox image is present
        - database: check if DATABASE_URL is set (placeholder — real check needs pool)
        - event_bus: always healthy (in-process)

        Returns dict mapping component name to {status, message, latency_ms}.
        """
        import shutil
        import subprocess

        health: dict[str, dict[str, Any]] = {}

        # --- openrouter ---
        if not self._state.openrouter_api_key:
            health["openrouter"] = {
                "status": "unhealthy",
                "message": "API key not configured",
                "latency_ms": None,
            }
        else:
            result = await self._test_openrouter_key()
            if result.success:
                health["openrouter"] = {
                    "status": "healthy",
                    "message": "",
                    "latency_ms": result.latency_ms,
                }
            else:
                health["openrouter"] = {
                    "status": "unhealthy",
                    "message": result.error,
                    "latency_ms": result.latency_ms,
                }

        # --- github ---
        if not self._state.github_token:
            health["github"] = {
                "status": "unhealthy",
                "message": "API key not configured",
                "latency_ms": None,
            }
        else:
            result = await self._test_github_key()
            if result.success:
                health["github"] = {
                    "status": "healthy",
                    "message": "",
                    "latency_ms": result.latency_ms,
                }
            else:
                health["github"] = {
                    "status": "unhealthy",
                    "message": result.error,
                    "latency_ms": result.latency_ms,
                }

        # --- docker ---
        docker_available = shutil.which("docker") is not None
        if not docker_available:
            health["docker"] = {
                "status": "unhealthy",
                "message": "Docker not found in PATH",
                "latency_ms": None,
            }
        else:
            try:
                proc = subprocess.run(
                    ["docker", "image", "inspect", "forge-aider-sandbox:latest"],
                    capture_output=True,
                    timeout=5,
                )
                if proc.returncode == 0:
                    health["docker"] = {
                        "status": "healthy",
                        "message": "",
                        "latency_ms": None,
                    }
                else:
                    health["docker"] = {
                        "status": "unhealthy",
                        "message": "Sandbox image 'forge-aider-sandbox:latest' not found",
                        "latency_ms": None,
                    }
            except (subprocess.TimeoutExpired, OSError) as exc:
                health["docker"] = {
                    "status": "unhealthy",
                    "message": f"Docker probe failed: {exc}",
                    "latency_ms": None,
                }

        # --- database ---
        if os.environ.get("DATABASE_URL"):
            health["database"] = {
                "status": "healthy",
                "message": "",
                "latency_ms": None,
            }
        else:
            health["database"] = {
                "status": "unhealthy",
                "message": "DATABASE_URL not set",
                "latency_ms": None,
            }

        # --- event_bus ---
        health["event_bus"] = {
            "status": "healthy",
            "message": "",
            "latency_ms": None,
        }

        return health

    async def get_models(self) -> list[dict[str, str]]:
        """Fetch available models from OpenRouter, with TTL-based caching.

        Returns list of {id, name} dicts.
        Caches for self._state.model_cache_ttl_seconds.
        Raises if OpenRouter key not configured.
        """
        import time

        import httpx

        if not self._state.openrouter_api_key:
            raise ValueError("OpenRouter API key not configured")

        now = time.time()
        ttl = self._state.model_cache_ttl_seconds

        # Return cached results if still fresh
        if (
            self._model_cache is not None
            and (now - self._model_cache_time) < ttl
        ):
            return self._model_cache

        # Fetch from OpenRouter
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={
                    "Authorization": f"Bearer {self._state.openrouter_api_key}"
                },
            )
            response.raise_for_status()

        data = response.json()
        models = [
            {"id": m["id"], "name": m.get("name", m["id"])}
            for m in data.get("data", [])
        ]

        # Cache results
        self._model_cache = models
        self._model_cache_time = time.time()

        return models

    async def _test_openrouter_key(self, key: str | None = None) -> KeyTestResult:
        """Test an OpenRouter API key by fetching the models endpoint.

        Makes a GET request to https://openrouter.ai/api/v1/models with
        the provided key as a Bearer token. Returns model count on success.
        """
        import time

        import httpx

        actual_key = key if key is not None else self._state.openrouter_api_key

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {actual_key}"},
                )
            latency_ms = (time.perf_counter() - start) * 1000

            if response.status_code in (401, 403):
                return KeyTestResult(
                    success=False,
                    latency_ms=latency_ms,
                    error="Invalid credentials",
                )

            response.raise_for_status()
            data = response.json()
            model_count = len(data.get("data", []))
            return KeyTestResult(
                success=True,
                latency_ms=latency_ms,
                details={"models_available": model_count},
            )

        except httpx.TimeoutException:
            latency_ms = (time.perf_counter() - start) * 1000
            return KeyTestResult(
                success=False,
                latency_ms=latency_ms,
                error="Request timed out after 10 seconds",
            )
        except httpx.HTTPStatusError as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return KeyTestResult(
                success=False,
                latency_ms=latency_ms,
                error=f"HTTP error: {exc.response.status_code}",
            )

    async def _test_github_key(self, key: str | None = None) -> KeyTestResult:
        """Test a GitHub token by fetching the authenticated user endpoint.

        Makes a GET request to https://api.github.com/user with the
        provided token as a Bearer token. Returns username on success.
        """
        import time

        import httpx

        actual_key = key if key is not None else self._state.github_token

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {actual_key}"},
                )
            latency_ms = (time.perf_counter() - start) * 1000

            if response.status_code == 401:
                return KeyTestResult(
                    success=False,
                    latency_ms=latency_ms,
                    error="Invalid credentials",
                )

            response.raise_for_status()
            data = response.json()
            username = data.get("login", "")
            return KeyTestResult(
                success=True,
                latency_ms=latency_ms,
                details={"username": username},
            )

        except httpx.TimeoutException:
            latency_ms = (time.perf_counter() - start) * 1000
            return KeyTestResult(
                success=False,
                latency_ms=latency_ms,
                error="Request timed out after 10 seconds",
            )
        except httpx.HTTPStatusError as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            return KeyTestResult(
                success=False,
                latency_ms=latency_ms,
                error=f"HTTP error: {exc.response.status_code}",
            )

    async def _save(self) -> None:
        """Persist current state to the config file atomically.

        Writes to a temporary file in the same directory, then renames
        to the target path. Sets restrictive permissions (0600) so only
        the owner can read/write the config (protects secrets).
        """
        data = self._serialize(self._state)
        content = json.dumps(data, indent=2, ensure_ascii=False)

        # Ensure parent directory exists
        self._config_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file in the same directory for atomic rename
        tmp_path = self._config_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")

            # Set restrictive permissions before rename (owner read/write only)
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                # On Windows, chmod may not fully work — best effort
                pass

            # Atomic rename
            os.replace(str(tmp_path), str(self._config_path))
            logger.info("Configuration saved to %s", self._config_path)
        except OSError as exc:
            # Clean up temp file on failure
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            logger.error("Failed to save config: %s", exc)
            raise

    def _serialize(self, state: ConfigState) -> dict[str, Any]:
        """Serialize ConfigState to a JSON-compatible dict."""
        data = asdict(state)
        # Enum values need to be stored as strings
        data["sandbox_mode"] = state.sandbox_mode.value
        return data

    def _deserialize(self, data: dict[str, Any]) -> ConfigState:
        """Deserialize a dict (from JSON) into a ConfigState.

        Handles missing keys gracefully by using defaults, and validates
        enum values.

        Args:
            data: Raw dictionary loaded from JSON.

        Returns:
            A valid ConfigState instance.

        Raises:
            ValueError: If sandbox_mode has an unrecognized value.
        """
        sandbox_raw = data.get("sandbox_mode", SandboxMode.AUTO.value)
        try:
            sandbox_mode = SandboxMode(sandbox_raw)
        except ValueError:
            raise ValueError(
                f"Invalid sandbox_mode '{sandbox_raw}'. "
                f"Must be one of: {', '.join(m.value for m in SandboxMode)}"
            )

        ttl = data.get("model_cache_ttl_seconds", 3600)
        if not isinstance(ttl, int) or ttl <= 0:
            ttl = 3600

        return ConfigState(
            openrouter_api_key=data.get("openrouter_api_key", ""),
            github_token=data.get("github_token", ""),
            selected_model=data.get("selected_model", ""),
            sandbox_mode=sandbox_mode,
            model_cache_ttl_seconds=ttl,
        )

    async def _emit_config_error(
        self,
        code: str,
        message: str,
        component: str,
        recoverable: bool,
        suggestion: str | None = None,
    ) -> None:
        """Emit a CONFIG_ERROR event on the event bus.

        Uses EventType.ERROR as a placeholder until CONFIG_ERROR is added
        to the EventType enum (task 5.1).
        """
        from datetime import datetime, timezone
        import uuid

        payload: dict[str, Any] = {
            "code": code,
            "message": message,
            "category": "configuration",
            "component": component,
            "recoverable": recoverable,
        }
        if suggestion:
            payload["suggestion"] = suggestion

        event = Event(
            schema_version=1,
            seq=0,
            session_id="system",
            type=EventType.CONFIG_ERROR,
            timestamp=datetime.now(timezone.utc),
            source="config_service",
            payload=payload,
            correlation_id="system",
            event_id=str(uuid.uuid4()),
        )

        try:
            await self._event_emitter(event)
        except Exception as exc:
            # Don't let event emission failure break config loading
            logger.warning("Failed to emit config error event: %s", exc)
