"""Secret handling for the Forge Runtime.

Implements per-session in-memory secret storage with redaction at every
serialization boundary. Secrets (VCS tokens, provider API keys) are held
only in memory and never written to disk, the relational store, the audit
trail, or any log.

Requirements: 22.1, 22.2, 22.3, 22.5, 22.6, 22.7
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# Masked placeholder used to replace raw secret values in persisted output.
REDACTED_PLACEHOLDER = "***REDACTED***"


class RedactionError(Exception):
    """Raised when redaction cannot be applied before a persistence operation.

    Per requirement 22.7, the operation must be aborted and no raw secret
    value may appear in the target.
    """

    def __init__(self, message: str = "Redaction could not be applied") -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class TokenReference:
    """A non-secret reference to a stored token.

    Stored in state snapshots instead of the raw token value (requirement 22.3).
    The reference is not reversible — it cannot be used to recover the secret.
    """

    session_id: str
    key_name: str
    placeholder: str = REDACTED_PLACEHOLDER

    def __str__(self) -> str:
        return f"TokenRef({self.key_name}@{self.session_id})"


@dataclass
class SecretHolder:
    """Per-session in-memory secret storage with redaction.

    Stores VCS tokens and provider API keys per session. Provides redaction
    utilities that replace raw secret values with masked placeholders before
    any persistence operation.

    Thread-safety note: In the asyncio single-threaded model used by Forge,
    no locking is needed. If multi-threaded access is required in the future,
    a lock should be added around mutations.
    """

    # Internal storage: session_id -> {key_name -> secret_value}
    _store: dict[str, dict[str, str]] = field(default_factory=dict)

    # ──────────────────────────────────────────────────────────────────────
    # Secret storage operations
    # ──────────────────────────────────────────────────────────────────────

    def store_secret(self, session_id: str, key_name: str, value: str) -> None:
        """Store a secret (VCS token or provider key) for a session.

        The secret is held only in memory — never persisted to disk or DB.
        """
        if not session_id:
            raise ValueError("session_id must not be empty")
        if not key_name:
            raise ValueError("key_name must not be empty")
        if not value:
            raise ValueError("secret value must not be empty")

        if session_id not in self._store:
            self._store[session_id] = {}
        self._store[session_id][key_name] = value

    def get_secret(self, session_id: str, key_name: str) -> str | None:
        """Retrieve a raw secret value. Returns None if not found.

        This should only be used internally (e.g., for VCS operations
        performed by the runtime itself, per requirement 22.5).
        """
        session_secrets = self._store.get(session_id)
        if session_secrets is None:
            return None
        return session_secrets.get(key_name)

    def has_secret(self, session_id: str, key_name: str) -> bool:
        """Check if a secret exists for the given session and key name."""
        session_secrets = self._store.get(session_id)
        if session_secrets is None:
            return False
        return key_name in session_secrets

    def list_keys(self, session_id: str) -> list[str]:
        """List all secret key names stored for a session."""
        session_secrets = self._store.get(session_id)
        if session_secrets is None:
            return []
        return list(session_secrets.keys())

    # ──────────────────────────────────────────────────────────────────────
    # Token references (requirement 22.3)
    # ──────────────────────────────────────────────────────────────────────

    def get_token_reference(self, session_id: str, key_name: str = "vcs_token") -> TokenReference:
        """Return a non-secret token reference for use in state snapshots.

        The reference replaces the raw value in any persisted state.
        Raises ValueError if no secret is stored for the given key.
        """
        if not self.has_secret(session_id, key_name):
            raise ValueError(
                f"No secret stored for session={session_id!r}, key={key_name!r}"
            )
        return TokenReference(session_id=session_id, key_name=key_name)

    # ──────────────────────────────────────────────────────────────────────
    # Redaction (requirements 22.2, 22.7)
    # ──────────────────────────────────────────────────────────────────────

    def _get_all_secret_values(self, session_id: str | None = None) -> set[str]:
        """Collect all raw secret values, optionally scoped to a session."""
        values: set[str] = set()
        if session_id is not None:
            session_secrets = self._store.get(session_id)
            if session_secrets:
                values.update(session_secrets.values())
        else:
            for session_secrets in self._store.values():
                values.update(session_secrets.values())
        return values

    def redact(self, data: Any, *, session_id: str | None = None) -> Any:
        """Replace all raw secret values with masked placeholders.

        Recursively traverses dicts, lists, tuples, and strings. Any
        occurrence of a stored secret value is replaced with REDACTED_PLACEHOLDER.

        Args:
            data: The data to redact. Can be a string, dict, list, or nested structure.
            session_id: If provided, only redact secrets for this session.
                        If None, redact all known secrets across all sessions.

        Returns:
            A new data structure with all secrets replaced by placeholders.
        """
        secret_values = self._get_all_secret_values(session_id)
        if not secret_values:
            return data
        return self._redact_recursive(data, secret_values)

    def _redact_recursive(self, data: Any, secret_values: set[str]) -> Any:
        """Recursively replace secret values in a data structure."""
        if isinstance(data, str):
            result = data
            for secret in secret_values:
                if secret in result:
                    result = result.replace(secret, REDACTED_PLACEHOLDER)
            return result
        elif isinstance(data, dict):
            return {
                self._redact_recursive(k, secret_values): self._redact_recursive(v, secret_values)
                for k, v in data.items()
            }
        elif isinstance(data, list):
            return [self._redact_recursive(item, secret_values) for item in data]
        elif isinstance(data, tuple):
            return tuple(self._redact_recursive(item, secret_values) for item in data)
        else:
            return data

    def redact_or_raise(self, data: Any, *, session_id: str | None = None) -> Any:
        """Redact secrets, aborting with RedactionError if redaction cannot be applied.

        This is the serialization hook for persistence operations. Per requirement
        22.7, if any raw secret still appears after redaction, the operation is
        aborted.

        Args:
            data: The data to redact before persistence.
            session_id: Optional session scope for redaction.

        Returns:
            Redacted data safe for persistence.

        Raises:
            RedactionError: If a raw secret value still appears after redaction
                            (indicating redaction could not be fully applied).
        """
        secret_values = self._get_all_secret_values(session_id)
        if not secret_values:
            return data

        redacted = self._redact_recursive(data, secret_values)

        # Verify no raw secrets remain in the redacted output
        if self._contains_secrets(redacted, secret_values):
            raise RedactionError(
                "Raw secret value detected after redaction attempt — "
                "persistence operation aborted"
            )

        return redacted

    def _contains_secrets(self, data: Any, secret_values: set[str]) -> bool:
        """Check if any raw secret value appears in the data structure."""
        if isinstance(data, str):
            return any(secret in data for secret in secret_values)
        elif isinstance(data, dict):
            for k, v in data.items():
                if self._contains_secrets(k, secret_values):
                    return True
                if self._contains_secrets(v, secret_values):
                    return True
            return False
        elif isinstance(data, (list, tuple)):
            return any(self._contains_secrets(item, secret_values) for item in data)
        return False

    # ──────────────────────────────────────────────────────────────────────
    # Session lifecycle (requirement 22.6)
    # ──────────────────────────────────────────────────────────────────────

    def clear_session(self, session_id: str) -> None:
        """Clear all secrets for a session on termination.

        After this call, no plaintext secret value is retained for the session.
        """
        self._store.pop(session_id, None)

    def clear_all(self) -> None:
        """Clear all secrets for all sessions. Used during shutdown."""
        self._store.clear()

    # ──────────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────────

    def session_count(self) -> int:
        """Return the number of sessions with stored secrets."""
        return len(self._store)

    def has_session(self, session_id: str) -> bool:
        """Check if any secrets are stored for the given session."""
        return session_id in self._store and len(self._store[session_id]) > 0
