"""Unit tests for shared types used across layers.

Tests Health, HealthStatus, ToolResult, and PermanentError from app.shared.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.shared import Health, HealthStatus, PermanentError, ToolResult

# ---------------------------------------------------------------------------
# HealthStatus Tests
# ---------------------------------------------------------------------------


class TestHealthStatus:
    """Tests for HealthStatus enum."""

    def test_health_status_values(self) -> None:
        """HealthStatus enum has expected values."""
        assert HealthStatus.OK.value == "ok"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"

    def test_health_status_is_string_enum(self) -> None:
        """HealthStatus inherits from str."""
        assert isinstance(HealthStatus.OK, str)


# ---------------------------------------------------------------------------
# Health Tests
# ---------------------------------------------------------------------------


class TestHealth:
    """Tests for Health dataclass."""

    def test_healthy_factory(self) -> None:
        """Health.healthy creates a healthy Health object."""
        health = Health.healthy(latency_ms=10.5)

        assert health.ok is True
        assert health.status == HealthStatus.OK
        assert health.latency_ms == 10.5
        assert health.message == ""
        assert health.timestamp is not None

    def test_healthy_factory_without_latency(self) -> None:
        """Health.healthy works without latency_ms."""
        health = Health.healthy()

        assert health.ok is True
        assert health.latency_ms is None

    def test_unhealthy_factory(self) -> None:
        """Health.unhealthy creates an unhealthy Health object."""
        health = Health.unhealthy(message="connection refused", latency_ms=50.0)

        assert health.ok is False
        assert health.status == HealthStatus.UNHEALTHY
        assert health.latency_ms == 50.0
        assert health.message == "connection refused"

    def test_unhealthy_factory_without_latency(self) -> None:
        """Health.unhealthy works without latency_ms."""
        health = Health.unhealthy(message="timeout")

        assert health.ok is False
        assert health.latency_ms is None
        assert health.message == "timeout"

    def test_degraded_factory(self) -> None:
        """Health.degraded creates a degraded Health object."""
        health = Health.degraded(message="high latency", latency_ms=500.0)

        assert health.ok is False
        assert health.status == HealthStatus.DEGRADED
        assert health.latency_ms == 500.0
        assert health.message == "high latency"

    def test_degraded_factory_without_latency(self) -> None:
        """Health.degraded works without latency_ms."""
        health = Health.degraded(message="degraded")

        assert health.ok is False
        assert health.latency_ms is None

    def test_health_is_frozen(self) -> None:
        """Health is a frozen dataclass."""
        health = Health.healthy()
        with pytest.raises(AttributeError):
            health.ok = False  # type: ignore

    def test_health_has_timestamp(self) -> None:
        """Health has a timestamp that is set by default."""
        before = datetime.now(timezone.utc)
        health = Health.healthy()
        after = datetime.now(timezone.utc)

        assert isinstance(health.timestamp, datetime)
        assert before <= health.timestamp <= after

    def test_health_equality(self) -> None:
        """Health objects with same values are equal."""
        # Create timestamps to ensure they're equal
        ts = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        health1 = Health(ok=True, status=HealthStatus.OK, latency_ms=10.0, timestamp=ts)
        health2 = Health(ok=True, status=HealthStatus.OK, latency_ms=10.0, timestamp=ts)

        assert health1 == health2

    def test_health_inequality(self) -> None:
        """Health objects with different values are not equal."""
        health1 = Health(ok=True, latency_ms=10.0)
        health2 = Health(ok=True, latency_ms=20.0)

        assert health1 != health2


# ---------------------------------------------------------------------------
# ToolResult Tests
# ---------------------------------------------------------------------------


class TestToolResult:
    """Tests for ToolResult dataclass."""

    def test_successful_result(self) -> None:
        """ToolResult with success=True creates a successful result."""
        result = ToolResult(
            success=True,
            files_modified=["file1.py", "file2.py"],
            output="Built 2 files",
        )

        assert result.success is True
        assert result.files_modified == ["file1.py", "file2.py"]
        assert result.output == "Built 2 files"
        assert result.error == ""
        assert result.metadata == {}

    def test_failed_result(self) -> None:
        """ToolResult with success=False creates a failed result."""
        result = ToolResult(
            success=False,
            error="Command failed: syntax error",
            output="",
        )

        assert result.success is False
        assert result.error == "Command failed: syntax error"
        assert result.output == ""

    def test_result_with_metadata(self) -> None:
        """ToolResult can include metadata."""
        result = ToolResult(
            success=True,
            files_modified=["file.py"],
            metadata={
                "duration_ms": 1500,
                "model": "gpt-4",
            },
        )

        assert result.metadata["duration_ms"] == 1500
        assert result.metadata["model"] == "gpt-4"

    def test_result_defaults(self) -> None:
        """ToolResult has correct default values."""
        result = ToolResult(success=True)

        assert result.files_modified == []
        assert result.output == ""
        assert result.error == ""
        assert result.metadata == {}

    def test_tool_result_is_frozen(self) -> None:
        """ToolResult is a frozen dataclass."""
        result = ToolResult(success=True)
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore


# ---------------------------------------------------------------------------
# PermanentError Tests
# ---------------------------------------------------------------------------


class TestPermanentError:
    """Tests for PermanentError exception."""

    def test_basic_error(self) -> None:
        """PermanentError can be raised with a message."""
        error = PermanentError("Invalid API key")

        assert str(error) == "Invalid API key"
        assert error.provider == ""
        assert error.error_type == ""

    def test_error_with_provider(self) -> None:
        """PermanentError can include provider information."""
        error = PermanentError(
            "Invalid API key for OpenAI",
            provider="openai",
        )

        assert str(error) == "Invalid API key for OpenAI"
        assert error.provider == "openai"

    def test_error_with_error_type(self) -> None:
        """PermanentError can include error type information."""
        error = PermanentError(
            "Malformed request",
            error_type="validation_error",
        )

        assert str(error) == "Malformed request"
        assert error.error_type == "validation_error"

    def test_error_with_all_fields(self) -> None:
        """PermanentError can include all information."""
        error = PermanentError(
            "Authentication failed",
            provider="github",
            error_type="auth_failure",
        )

        assert str(error) == "Authentication failed"
        assert error.provider == "github"
        assert error.error_type == "auth_failure"

    def test_error_is_exception(self) -> None:
        """PermanentError is an Exception subclass."""
        error = PermanentError("test")
        assert isinstance(error, Exception)

    def test_error_can_be_caught(self) -> None:
        """PermanentError can be caught with except Exception."""
        with pytest.raises(Exception) as exc_info:
            raise PermanentError("Caught this")

        assert str(exc_info.value) == "Caught this"

    def test_error_can_be_caught_as_permanent_error(self) -> None:
        """PermanentError can be caught as PermanentError."""
        with pytest.raises(PermanentError) as exc_info:
            raise PermanentError("Caught this", provider="test")

        assert str(exc_info.value) == "Caught this"
        assert exc_info.value.provider == "test"
