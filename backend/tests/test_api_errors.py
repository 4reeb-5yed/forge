"""Unit tests for the Application Layer error handling module.

Tests ErrorEnvelope, ErrorCategory, status code mapping functions,
and exception handlers.

Requirements: 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.errors import (
    ErrorCategory,
    ErrorEnvelope,
    _category_from_status,
    _code_from_status,
    _handle_config_validation_error,
    _handle_http_exception,
    _handle_unhandled_exception,
    register_error_handlers,
)
from app.runtime.config import ConfigValidationError

# ---------------------------------------------------------------------------
# ErrorCategory Tests
# ---------------------------------------------------------------------------


class TestErrorCategory:
    """Tests for ErrorCategory enum."""

    def test_error_category_values(self) -> None:
        """ErrorCategory enum has expected values."""
        assert ErrorCategory.CONFIGURATION.value == "configuration"
        assert ErrorCategory.RUNTIME.value == "runtime"
        assert ErrorCategory.WORKFLOW.value == "workflow"
        assert ErrorCategory.CONNECTION.value == "connection"

    def test_error_category_is_string_enum(self) -> None:
        """ErrorCategory inherits from str."""
        assert isinstance(ErrorCategory.CONFIGURATION, str)


# ---------------------------------------------------------------------------
# ErrorEnvelope Tests
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    """Tests for ErrorEnvelope dataclass."""

    def test_create_with_required_fields(self) -> None:
        """ErrorEnvelope can be created with required fields only."""
        envelope = ErrorEnvelope(
            code="TEST_ERROR",
            message="Test message",
            category=ErrorCategory.RUNTIME,
            recoverable=True,
        )
        assert envelope.code == "TEST_ERROR"
        assert envelope.message == "Test message"
        assert envelope.category == ErrorCategory.RUNTIME
        assert envelope.recoverable is True
        assert envelope.suggestion is None

    def test_create_with_all_fields(self) -> None:
        """ErrorEnvelope can be created with all fields."""
        envelope = ErrorEnvelope(
            code="FULL_ERROR",
            message="Full error message",
            category=ErrorCategory.CONFIGURATION,
            recoverable=False,
            suggestion="Check the config file.",
        )
        assert envelope.code == "FULL_ERROR"
        assert envelope.message == "Full error message"
        assert envelope.category == ErrorCategory.CONFIGURATION
        assert envelope.recoverable is False
        assert envelope.suggestion == "Check the config file."

    def test_timestamp_default_is_set(self) -> None:
        """ErrorEnvelope sets timestamp to current UTC time by default."""
        before = datetime.now(timezone.utc)
        envelope = ErrorEnvelope(
            code="TIMED",
            message="Test",
            category=ErrorCategory.RUNTIME,
            recoverable=True,
        )
        after = datetime.now(timezone.utc)

        assert envelope.timestamp is not None
        assert before <= datetime.fromisoformat(envelope.timestamp) <= after

    def test_timestamp_can_be_overridden(self) -> None:
        """ErrorEnvelope allows overriding the timestamp."""
        custom_ts = "2024-01-15T10:30:00+00:00"
        envelope = ErrorEnvelope(
            code="TIMED",
            message="Test",
            category=ErrorCategory.RUNTIME,
            recoverable=True,
            timestamp=custom_ts,
        )
        assert envelope.timestamp == custom_ts

    def test_to_dict_with_suggestion(self) -> None:
        """to_dict includes suggestion when it is not None."""
        envelope = ErrorEnvelope(
            code="WITH_SUGGESTION",
            message="Error with hint",
            category=ErrorCategory.CONFIGURATION,
            recoverable=True,
            suggestion="Try restarting the service.",
        )
        result = envelope.to_dict()

        assert result["code"] == "WITH_SUGGESTION"
        assert result["message"] == "Error with hint"
        assert result["category"] == "configuration"
        assert result["recoverable"] is True
        assert result["suggestion"] == "Try restarting the service."
        assert "timestamp" in result

    def test_to_dict_without_suggestion(self) -> None:
        """to_dict omits suggestion when it is None."""
        envelope = ErrorEnvelope(
            code="NO_SUGGESTION",
            message="Error without hint",
            category=ErrorCategory.RUNTIME,
            recoverable=False,
        )
        result = envelope.to_dict()

        assert result["code"] == "NO_SUGGESTION"
        assert "suggestion" not in result

    def test_to_dict_contains_all_required_fields(self) -> None:
        """to_dict returns all required fields."""
        envelope = ErrorEnvelope(
            code="COMPLETE",
            message="Complete error",
            category=ErrorCategory.CONNECTION,
            recoverable=True,
        )
        result = envelope.to_dict()

        assert set(result.keys()) == {
            "code",
            "message",
            "category",
            "recoverable",
            "timestamp",
        }


# ---------------------------------------------------------------------------
# Status Code Mapping Tests
# ---------------------------------------------------------------------------


class TestCategoryFromStatus:
    """Tests for _category_from_status function."""

    def test_401_returns_configuration(self) -> None:
        """401 Unauthorized maps to CONFIGURATION."""
        assert _category_from_status(401) == ErrorCategory.CONFIGURATION

    def test_403_returns_configuration(self) -> None:
        """403 Forbidden maps to CONFIGURATION."""
        assert _category_from_status(403) == ErrorCategory.CONFIGURATION

    def test_422_returns_configuration(self) -> None:
        """422 Unprocessable Entity maps to CONFIGURATION."""
        assert _category_from_status(422) == ErrorCategory.CONFIGURATION

    def test_500_returns_runtime(self) -> None:
        """500 Internal Server Error maps to RUNTIME."""
        assert _category_from_status(500) == ErrorCategory.RUNTIME

    def test_502_returns_runtime(self) -> None:
        """502 Bad Gateway maps to RUNTIME."""
        assert _category_from_status(502) == ErrorCategory.RUNTIME

    def test_503_returns_runtime(self) -> None:
        """503 Service Unavailable maps to RUNTIME."""
        assert _category_from_status(503) == ErrorCategory.RUNTIME

    def test_504_returns_runtime(self) -> None:
        """504 Gateway Timeout maps to RUNTIME."""
        assert _category_from_status(504) == ErrorCategory.RUNTIME

    def test_400_returns_runtime(self) -> None:
        """400 Bad Request maps to RUNTIME (default for 4xx not explicitly handled)."""
        assert _category_from_status(400) == ErrorCategory.RUNTIME


class TestCodeFromStatus:
    """Tests for _code_from_status function."""

    def test_400_returns_bad_request(self) -> None:
        assert _code_from_status(400) == "BAD_REQUEST"

    def test_401_returns_authentication_failure(self) -> None:
        assert _code_from_status(401) == "AUTHENTICATION_FAILURE"

    def test_403_returns_forbidden(self) -> None:
        assert _code_from_status(403) == "FORBIDDEN"

    def test_404_returns_not_found(self) -> None:
        assert _code_from_status(404) == "NOT_FOUND"

    def test_409_returns_conflict(self) -> None:
        assert _code_from_status(409) == "CONFLICT"

    def test_422_returns_validation_error(self) -> None:
        assert _code_from_status(422) == "VALIDATION_ERROR"

    def test_429_returns_rate_limited(self) -> None:
        assert _code_from_status(429) == "RATE_LIMITED"

    def test_500_returns_internal_error(self) -> None:
        assert _code_from_status(500) == "INTERNAL_ERROR"

    def test_502_returns_bad_gateway(self) -> None:
        assert _code_from_status(502) == "BAD_GATEWAY"

    def test_503_returns_service_unavailable(self) -> None:
        assert _code_from_status(503) == "SERVICE_UNAVAILABLE"

    def test_unknown_code_returns_http_prefix(self) -> None:
        """Unknown status codes return HTTP_<code> format."""
        assert _code_from_status(418) == "HTTP_418"
        assert _code_from_status(599) == "HTTP_599"


# ---------------------------------------------------------------------------
# Exception Handler Tests
# ---------------------------------------------------------------------------


class TestHandleHttpException:
    """Tests for _handle_http_exception function."""

    @pytest.fixture
    def mock_request(self) -> Request:
        """Create a mock Request object."""
        request = MagicMock(spec=Request)
        request.method = "GET"
        request.url.path = "/test"
        return request

    async def test_401_exception_with_suggestion(
        self, mock_request: Request
    ) -> None:
        """401 exception includes auth configuration suggestion."""
        exc = HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
        response = await _handle_http_exception(mock_request, exc)

        assert response.status_code == 401
        body = response.body.decode()
        assert "AUTHENTICATION_FAILURE" in body
        assert "Invalid token" in body
        assert "configuration" in body
        assert "Check your API token" in body

    async def test_403_exception_with_suggestion(
        self, mock_request: Request
    ) -> None:
        """403 exception includes auth configuration suggestion."""
        exc = HTTPException(status_code=403, detail="Access denied")
        response = await _handle_http_exception(mock_request, exc)

        assert response.status_code == 403
        body = response.body.decode()
        assert "FORBIDDEN" in body
        assert "configuration" in body

    async def test_404_exception_no_suggestion(
        self, mock_request: Request
    ) -> None:
        """404 exception does not include suggestion."""
        exc = HTTPException(status_code=404, detail="Session not found")
        response = await _handle_http_exception(mock_request, exc)

        assert response.status_code == 404
        body = response.body.decode()
        assert "NOT_FOUND" in body
        assert "suggestion" not in body

    async def test_500_exception_not_recoverable(
        self, mock_request: Request
    ) -> None:
        """500 exception marks response as not recoverable."""
        exc = HTTPException(status_code=500, detail="Internal error")
        response = await _handle_http_exception(mock_request, exc)

        assert response.status_code == 500
        body = response.body.decode()
        assert "recoverable" in body
        # JSON encoding may produce lowercase or different formatting
        assert "false" in body or "False" in body

    async def test_detail_as_dict(self, mock_request: Request) -> None:
        """Handles HTTPException with dict detail."""
        exc = HTTPException(
            status_code=422,
            detail={"detail": "Validation failed", "field": "email"},
        )
        response = await _handle_http_exception(mock_request, exc)

        assert response.status_code == 422
        body = response.body.decode()
        assert "Validation failed" in body

    async def test_detail_as_list(self, mock_request: Request) -> None:
        """Handles HTTPException with non-standard detail type."""
        exc = HTTPException(status_code=422, detail=["error1", "error2"])
        response = await _handle_http_exception(mock_request, exc)

        assert response.status_code == 422
        body = response.body.decode()
        assert "VALIDATION_ERROR" in body


class TestHandleConfigValidationError:
    """Tests for _handle_config_validation_error function."""

    @pytest.fixture
    def mock_request(self) -> Request:
        """Create a mock Request object."""
        return MagicMock(spec=Request)

    async def test_returns_422_status(self, mock_request: Request) -> None:
        """Returns 422 Unprocessable Entity status."""
        exc = ConfigValidationError(errors={})
        response = await _handle_config_validation_error(mock_request, exc)

        assert response.status_code == 422

    async def test_includes_field_errors(self, mock_request: Request) -> None:
        """Includes validation field errors in response."""
        exc = ConfigValidationError(
            errors={
                "email": "Invalid email format",
                "password": "Must be at least 8 characters",
            }
        )
        response = await _handle_config_validation_error(mock_request, exc)

        body = response.body.decode()
        assert "VALIDATION_ERROR" in body
        assert '"fields"' in body
        assert "email" in body
        assert "password" in body

    async def test_is_recoverable(self, mock_request: Request) -> None:
        """ConfigValidationError is marked as recoverable."""
        exc = ConfigValidationError(errors={})
        response = await _handle_config_validation_error(mock_request, exc)

        body = response.body.decode()
        assert "recoverable" in body
        # JSON encoding may produce lowercase or different formatting
        assert "true" in body or "True" in body

    async def test_has_suggestion(self, mock_request: Request) -> None:
        """ConfigValidationError includes suggestion for fixing."""
        exc = ConfigValidationError(errors={})
        response = await _handle_config_validation_error(mock_request, exc)

        body = response.body.decode()
        assert "suggestion" in body
        assert "Check the invalid fields" in body


class TestHandleUnhandledException:
    """Tests for _handle_unhandled_exception function."""

    @pytest.fixture
    def mock_request(self) -> Request:
        """Create a mock Request object."""
        request = MagicMock(spec=Request)
        request.method = "POST"
        request.url.path = "/api/sessions"
        return request

    async def test_returns_500_status(self, mock_request: Request) -> None:
        """Unhandled exceptions return 500 status."""
        exc = ValueError("Unexpected error")
        response = await _handle_unhandled_exception(mock_request, exc)

        assert response.status_code == 500

    async def test_generic_message(self, mock_request: Request) -> None:
        """Generic error message is returned, not exposing internal details."""
        exc = ValueError("Database connection failed: host=localhost port=5432")
        response = await _handle_unhandled_exception(mock_request, exc)

        body = response.body.decode()
        assert "INTERNAL_ERROR" in body
        assert "unexpected error" in body.lower()
        # Original error message should not be exposed
        assert "Database connection" not in body
        assert "localhost" not in body

    async def test_not_recoverable(self, mock_request: Request) -> None:
        """Unhandled exceptions are marked as not recoverable."""
        exc = RuntimeError("Oops")
        response = await _handle_unhandled_exception(mock_request, exc)

        body = response.body.decode()
        assert "recoverable" in body
        # JSON encoding may produce lowercase or different formatting
        assert "false" in body or "False" in body


# ---------------------------------------------------------------------------
# Register Error Handlers Tests
# ---------------------------------------------------------------------------


class TestRegisterErrorHandlers:
    """Tests for register_error_handlers function."""

    def test_registers_http_exception_handler(self) -> None:
        """HTTPException handler is registered."""
        app = FastAPI()
        register_error_handlers(app)

        assert HTTPException in app.exception_handlers

    def test_registers_config_validation_error_handler(self) -> None:
        """ConfigValidationError handler is registered."""
        app = FastAPI()
        register_error_handlers(app)

        assert ConfigValidationError in app.exception_handlers

    def test_registers_generic_exception_handler(self) -> None:
        """Generic Exception handler is registered."""
        app = FastAPI()
        register_error_handlers(app)

        assert Exception in app.exception_handlers

    def test_handlers_can_be_overridden(self) -> None:
        """New handlers can be added after register_error_handlers."""
        app = FastAPI()

        async def custom_handler(request, exc):
            return JSONResponse(status_code=999, content={"custom": True})

        register_error_handlers(app)
        app.add_exception_handler(HTTPException, custom_handler)

        # The custom handler should be the one registered
        assert app.exception_handlers[HTTPException] == custom_handler
