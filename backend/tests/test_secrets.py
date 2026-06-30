"""Unit tests for the SecretHolder module.

Tests per-session in-memory secret storage, redaction at serialization
boundaries, token references, session clearing, and abort on redaction failure.

Requirements: 22.1, 22.2, 22.3, 22.5, 22.6, 22.7
"""

from __future__ import annotations

import pytest

from app.runtime.secrets import (
    REDACTED_PLACEHOLDER,
    RedactionError,
    SecretHolder,
    TokenReference,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def holder() -> SecretHolder:
    """A fresh SecretHolder instance."""
    return SecretHolder()


@pytest.fixture
def populated_holder() -> SecretHolder:
    """A SecretHolder with some secrets pre-loaded."""
    h = SecretHolder()
    h.store_secret("session-1", "vcs_token", "ghp_abc123secretXYZ")
    h.store_secret("session-1", "openai_key", "sk-test-key-456")
    h.store_secret("session-2", "vcs_token", "ghp_def789anotherToken")
    return h


# ──────────────────────────────────────────────────────────────────────────────
# Secret storage tests (Requirement 22.1)
# ──────────────────────────────────────────────────────────────────────────────


class TestSecretStorage:
    """Test per-session in-memory secret storage."""

    def test_store_and_retrieve_secret(self, holder: SecretHolder) -> None:
        holder.store_secret("s1", "vcs_token", "my-secret-token")
        assert holder.get_secret("s1", "vcs_token") == "my-secret-token"

    def test_store_multiple_keys_per_session(self, holder: SecretHolder) -> None:
        holder.store_secret("s1", "vcs_token", "token-val")
        holder.store_secret("s1", "api_key", "key-val")
        assert holder.get_secret("s1", "vcs_token") == "token-val"
        assert holder.get_secret("s1", "api_key") == "key-val"

    def test_store_secrets_for_multiple_sessions(self, holder: SecretHolder) -> None:
        holder.store_secret("s1", "vcs_token", "token-1")
        holder.store_secret("s2", "vcs_token", "token-2")
        assert holder.get_secret("s1", "vcs_token") == "token-1"
        assert holder.get_secret("s2", "vcs_token") == "token-2"

    def test_get_nonexistent_session_returns_none(self, holder: SecretHolder) -> None:
        assert holder.get_secret("missing-session", "vcs_token") is None

    def test_get_nonexistent_key_returns_none(self, holder: SecretHolder) -> None:
        holder.store_secret("s1", "vcs_token", "some-value")
        assert holder.get_secret("s1", "nonexistent_key") is None

    def test_has_secret_true(self, holder: SecretHolder) -> None:
        holder.store_secret("s1", "vcs_token", "val")
        assert holder.has_secret("s1", "vcs_token") is True

    def test_has_secret_false(self, holder: SecretHolder) -> None:
        assert holder.has_secret("s1", "vcs_token") is False

    def test_list_keys(self, populated_holder: SecretHolder) -> None:
        keys = populated_holder.list_keys("session-1")
        assert set(keys) == {"vcs_token", "openai_key"}

    def test_list_keys_empty_session(self, holder: SecretHolder) -> None:
        assert holder.list_keys("no-session") == []

    def test_store_empty_session_id_raises(self, holder: SecretHolder) -> None:
        with pytest.raises(ValueError, match="session_id"):
            holder.store_secret("", "key", "value")

    def test_store_empty_key_name_raises(self, holder: SecretHolder) -> None:
        with pytest.raises(ValueError, match="key_name"):
            holder.store_secret("s1", "", "value")

    def test_store_empty_value_raises(self, holder: SecretHolder) -> None:
        with pytest.raises(ValueError, match="secret value"):
            holder.store_secret("s1", "key", "")

    def test_overwrite_existing_secret(self, holder: SecretHolder) -> None:
        holder.store_secret("s1", "vcs_token", "old-value")
        holder.store_secret("s1", "vcs_token", "new-value")
        assert holder.get_secret("s1", "vcs_token") == "new-value"


# ──────────────────────────────────────────────────────────────────────────────
# Token reference tests (Requirement 22.3)
# ──────────────────────────────────────────────────────────────────────────────


class TestTokenReference:
    """Test token reference generation for state snapshots."""

    def test_get_token_reference(self, populated_holder: SecretHolder) -> None:
        ref = populated_holder.get_token_reference("session-1", "vcs_token")
        assert isinstance(ref, TokenReference)
        assert ref.session_id == "session-1"
        assert ref.key_name == "vcs_token"
        assert ref.placeholder == REDACTED_PLACEHOLDER

    def test_token_reference_does_not_contain_secret(
        self, populated_holder: SecretHolder
    ) -> None:
        ref = populated_holder.get_token_reference("session-1", "vcs_token")
        ref_str = str(ref)
        assert "ghp_abc123secretXYZ" not in ref_str
        assert "REDACTED" in ref_str or "TokenRef" in ref_str

    def test_get_token_reference_nonexistent_raises(self, holder: SecretHolder) -> None:
        with pytest.raises(ValueError, match="No secret stored"):
            holder.get_token_reference("no-session", "vcs_token")

    def test_token_reference_is_frozen(self, populated_holder: SecretHolder) -> None:
        ref = populated_holder.get_token_reference("session-1", "vcs_token")
        # Frozen dataclass — attribute assignment should raise
        with pytest.raises(Exception):
            ref.session_id = "hacked"  # type: ignore[misc]


# ──────────────────────────────────────────────────────────────────────────────
# Redaction tests (Requirement 22.2)
# ──────────────────────────────────────────────────────────────────────────────


class TestRedaction:
    """Test redaction of secrets in data structures."""

    def test_redact_string(self, populated_holder: SecretHolder) -> None:
        data = "Authorization: Bearer ghp_abc123secretXYZ"
        result = populated_holder.redact(data, session_id="session-1")
        assert "ghp_abc123secretXYZ" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_redact_dict_values(self, populated_holder: SecretHolder) -> None:
        data = {"token": "ghp_abc123secretXYZ", "name": "test"}
        result = populated_holder.redact(data, session_id="session-1")
        assert result["token"] == REDACTED_PLACEHOLDER
        assert result["name"] == "test"

    def test_redact_dict_keys(self, populated_holder: SecretHolder) -> None:
        data = {"ghp_abc123secretXYZ": "value"}
        result = populated_holder.redact(data, session_id="session-1")
        assert REDACTED_PLACEHOLDER in result
        assert "ghp_abc123secretXYZ" not in result

    def test_redact_nested_dict(self, populated_holder: SecretHolder) -> None:
        data = {
            "config": {
                "auth": {"token": "ghp_abc123secretXYZ"},
                "api": {"key": "sk-test-key-456"},
            }
        }
        result = populated_holder.redact(data, session_id="session-1")
        assert result["config"]["auth"]["token"] == REDACTED_PLACEHOLDER
        assert result["config"]["api"]["key"] == REDACTED_PLACEHOLDER

    def test_redact_list(self, populated_holder: SecretHolder) -> None:
        data = ["safe", "ghp_abc123secretXYZ", "also-safe"]
        result = populated_holder.redact(data, session_id="session-1")
        assert result == ["safe", REDACTED_PLACEHOLDER, "also-safe"]

    def test_redact_tuple(self, populated_holder: SecretHolder) -> None:
        data = ("safe", "ghp_abc123secretXYZ")
        result = populated_holder.redact(data, session_id="session-1")
        assert result == ("safe", REDACTED_PLACEHOLDER)
        assert result[0] == "safe"
        assert result[1] == REDACTED_PLACEHOLDER

    def test_redact_embedded_in_string(self, populated_holder: SecretHolder) -> None:
        data = "token=ghp_abc123secretXYZ&user=admin"
        result = populated_holder.redact(data, session_id="session-1")
        assert "ghp_abc123secretXYZ" not in result
        assert f"token={REDACTED_PLACEHOLDER}&user=admin" == result

    def test_redact_multiple_secrets_in_one_string(
        self, populated_holder: SecretHolder
    ) -> None:
        data = "vcs=ghp_abc123secretXYZ api=sk-test-key-456"
        result = populated_holder.redact(data, session_id="session-1")
        assert "ghp_abc123secretXYZ" not in result
        assert "sk-test-key-456" not in result

    def test_redact_no_secrets_returns_unchanged(self, holder: SecretHolder) -> None:
        data = {"key": "value", "list": [1, 2, 3]}
        result = holder.redact(data)
        assert result == data

    def test_redact_all_sessions(self, populated_holder: SecretHolder) -> None:
        # Without session_id, all secrets across all sessions are redacted
        data = "tokens: ghp_abc123secretXYZ and ghp_def789anotherToken"
        result = populated_holder.redact(data)
        assert "ghp_abc123secretXYZ" not in result
        assert "ghp_def789anotherToken" not in result

    def test_redact_non_string_non_container_passthrough(
        self, populated_holder: SecretHolder
    ) -> None:
        # Numbers, booleans, None should pass through unchanged
        assert populated_holder.redact(42, session_id="session-1") == 42
        assert populated_holder.redact(True, session_id="session-1") is True
        assert populated_holder.redact(None, session_id="session-1") is None

    def test_redact_scoped_to_session(self, populated_holder: SecretHolder) -> None:
        # Session-2's token should NOT be redacted when scoped to session-1
        data = "token: ghp_def789anotherToken"
        result = populated_holder.redact(data, session_id="session-1")
        assert "ghp_def789anotherToken" in result  # Not redacted — wrong session


# ──────────────────────────────────────────────────────────────────────────────
# Redact-or-raise tests (Requirement 22.7)
# ──────────────────────────────────────────────────────────────────────────────


class TestRedactOrRaise:
    """Test the serialization hook that aborts on redaction failure."""

    def test_redact_or_raise_succeeds(self, populated_holder: SecretHolder) -> None:
        data = {"token": "ghp_abc123secretXYZ"}
        result = populated_holder.redact_or_raise(data, session_id="session-1")
        assert result["token"] == REDACTED_PLACEHOLDER

    def test_redact_or_raise_no_secrets_passthrough(self, holder: SecretHolder) -> None:
        data = {"key": "value"}
        result = holder.redact_or_raise(data)
        assert result == data

    def test_redact_or_raise_complex_structure(
        self, populated_holder: SecretHolder
    ) -> None:
        data = {
            "state": {
                "session_id": "session-1",
                "auth": "ghp_abc123secretXYZ",
                "tasks": [{"name": "task1", "key": "sk-test-key-456"}],
            }
        }
        result = populated_holder.redact_or_raise(data, session_id="session-1")
        assert result["state"]["auth"] == REDACTED_PLACEHOLDER
        assert result["state"]["tasks"][0]["key"] == REDACTED_PLACEHOLDER
        assert result["state"]["session_id"] == "session-1"


# ──────────────────────────────────────────────────────────────────────────────
# Session lifecycle tests (Requirement 22.6)
# ──────────────────────────────────────────────────────────────────────────────


class TestSessionLifecycle:
    """Test secret clearing on session termination."""

    def test_clear_session_removes_all_secrets(
        self, populated_holder: SecretHolder
    ) -> None:
        populated_holder.clear_session("session-1")
        assert populated_holder.get_secret("session-1", "vcs_token") is None
        assert populated_holder.get_secret("session-1", "openai_key") is None
        assert populated_holder.has_session("session-1") is False

    def test_clear_session_does_not_affect_other_sessions(
        self, populated_holder: SecretHolder
    ) -> None:
        populated_holder.clear_session("session-1")
        # session-2 should still have its secrets
        assert populated_holder.get_secret("session-2", "vcs_token") == "ghp_def789anotherToken"

    def test_clear_nonexistent_session_is_noop(self, holder: SecretHolder) -> None:
        # Should not raise
        holder.clear_session("non-existent")

    def test_clear_all(self, populated_holder: SecretHolder) -> None:
        populated_holder.clear_all()
        assert populated_holder.session_count() == 0
        assert populated_holder.get_secret("session-1", "vcs_token") is None
        assert populated_holder.get_secret("session-2", "vcs_token") is None

    def test_session_count(self, populated_holder: SecretHolder) -> None:
        assert populated_holder.session_count() == 2

    def test_has_session(self, populated_holder: SecretHolder) -> None:
        assert populated_holder.has_session("session-1") is True
        assert populated_holder.has_session("nonexistent") is False

    def test_secrets_not_accessible_after_clear(
        self, populated_holder: SecretHolder
    ) -> None:
        """After clearing, redaction should not find any secrets for the session."""
        populated_holder.clear_session("session-1")
        data = "token: ghp_abc123secretXYZ"
        # Scoped to session-1: no secrets to redact, so string remains unchanged
        result = populated_holder.redact(data, session_id="session-1")
        # The secret value is still in the string, but it's no longer tracked
        # so redaction doesn't know about it. This is correct — the session
        # has ended, secrets are gone from memory.
        assert result == data


# ──────────────────────────────────────────────────────────────────────────────
# State snapshot redaction integration (Requirements 22.2, 22.3)
# ──────────────────────────────────────────────────────────────────────────────


class TestStateSnapshotRedaction:
    """Test that state snapshots never contain raw secrets."""

    def test_state_snapshot_with_token_reference(
        self, populated_holder: SecretHolder
    ) -> None:
        """Simulate a state snapshot using token references instead of raw values."""
        ref = populated_holder.get_token_reference("session-1", "vcs_token")
        snapshot = {
            "session_id": "session-1",
            "vcs_token": str(ref),  # Token reference, not raw value
            "status": "running",
        }
        # The snapshot should not contain the raw secret
        redacted = populated_holder.redact_or_raise(snapshot, session_id="session-1")
        assert "ghp_abc123secretXYZ" not in str(redacted)

    def test_raw_secret_in_snapshot_gets_redacted(
        self, populated_holder: SecretHolder
    ) -> None:
        """If a raw secret accidentally appears in a snapshot, it gets redacted."""
        snapshot = {
            "session_id": "session-1",
            "vcs_token": "ghp_abc123secretXYZ",  # Oops, raw value
            "status": "running",
        }
        redacted = populated_holder.redact_or_raise(snapshot, session_id="session-1")
        assert redacted["vcs_token"] == REDACTED_PLACEHOLDER
        assert redacted["session_id"] == "session-1"

    def test_event_payload_redaction(self, populated_holder: SecretHolder) -> None:
        """Event payloads should be redacted before persistence."""
        payload = {
            "type": "model.selected",
            "details": f"Using token ghp_abc123secretXYZ for auth",
        }
        redacted = populated_holder.redact_or_raise(payload, session_id="session-1")
        assert "ghp_abc123secretXYZ" not in redacted["details"]
        assert REDACTED_PLACEHOLDER in redacted["details"]


# ──────────────────────────────────────────────────────────────────────────────
# Additional gap-filling tests
# ──────────────────────────────────────────────────────────────────────────────


class TestSecretClearingEdgeCases:
    """Additional edge-case tests for session clearing and re-use."""

    def test_clear_then_store_new_secrets_same_session(self, holder: SecretHolder) -> None:
        """After clearing a session, it can be reused with fresh secrets."""
        holder.store_secret("s1", "vcs_token", "old-token-123")
        holder.clear_session("s1")
        assert holder.has_session("s1") is False

        # Re-use same session_id with new secret
        holder.store_secret("s1", "vcs_token", "new-token-456")
        assert holder.get_secret("s1", "vcs_token") == "new-token-456"
        assert holder.has_session("s1") is True

    def test_clear_session_then_global_redact_does_not_leak(self) -> None:
        """After clearing session-1, global redact still catches session-2 secrets."""
        holder = SecretHolder()
        holder.store_secret("s1", "key", "secret-A")
        holder.store_secret("s2", "key", "secret-B")
        holder.clear_session("s1")

        # Global redact (no session_id) should still redact s2's secret
        data = "values: secret-A and secret-B"
        result = holder.redact(data)
        # secret-A is no longer tracked (cleared), so it remains
        assert "secret-A" in result
        # secret-B is still active, so it gets redacted
        assert "secret-B" not in result
        assert REDACTED_PLACEHOLDER in result

    def test_redact_or_raise_raises_redaction_error(self) -> None:
        """RedactionError is raised when redaction cannot fully remove secrets.

        We simulate this by subclassing to inject a broken _redact_recursive
        that doesn't actually replace secrets, ensuring the safety check works.
        """
        class BrokenHolder(SecretHolder):
            """A holder whose redaction intentionally fails to replace."""
            def _redact_recursive(self, data, secret_values):
                # Intentionally return data unchanged — simulates redaction failure
                return data

        holder = BrokenHolder()
        holder.store_secret("s1", "vcs_token", "super-secret-value")

        with pytest.raises(RedactionError):
            holder.redact_or_raise(
                {"token": "super-secret-value"}, session_id="s1"
            )

    def test_redaction_error_message(self) -> None:
        """RedactionError carries a descriptive message."""
        err = RedactionError("test failure message")
        assert "test failure message" in str(err)
        assert err.message == "test failure message"

    def test_redaction_error_default_message(self) -> None:
        """RedactionError has a sensible default message."""
        err = RedactionError()
        assert "Redaction could not be applied" in str(err)
