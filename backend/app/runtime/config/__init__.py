"""ConfigService — runtime configuration manager for Forge.

Loads, validates, persists, and serves Forge configuration from a JSON file
on a mounted volume. Single source of truth for all config state.

Responsibilities:
- Load config from JSON file on startup
- Persist config atomically (write-tmp-rename) with restrictive permissions
- Emit CONFIG_ERROR events on failures (corrupt file, missing deps)
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.runtime.events.models import Event, EventType

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

    @property
    def state(self) -> ConfigState:
        """Current configuration state."""
        return self._state

    async def load(self) -> ConfigState:
        """Load configuration from the JSON file.

        Handles three cases:
        1. File exists and is valid JSON → deserialize to ConfigState
        2. File does not exist → use defaults (unconfigured)
        3. File exists but contains corrupt JSON → log error, use defaults,
           emit CONFIG_ERROR event

        Returns:
            The loaded (or default) ConfigState.
        """
        if not self._config_path.exists():
            logger.info(
                "Config file not found at %s, using defaults", self._config_path
            )
            self._state = ConfigState()
            return self._state

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._state = self._deserialize(data)
            logger.info("Configuration loaded from %s", self._config_path)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error(
                "Corrupt config file at %s: %s. Using defaults.",
                self._config_path,
                exc,
            )
            self._state = ConfigState()
            await self._emit_config_error(
                code="CONFIG_FILE_CORRUPT",
                message=f"Configuration file is corrupt: {exc}. Using defaults.",
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

        # TODO: apply_to_runtime() — will be implemented in task 1.3

        return await self.get_config()

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

        # Use EventType.ERROR as placeholder until CONFIG_ERROR is added (task 5.1)
        event = Event(
            schema_version=1,
            seq=0,
            session_id="system",
            type=EventType.ERROR,
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
