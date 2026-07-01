"""Structured error response models and exception handlers for the Forge API.

Defines the ErrorEnvelope and ErrorCategory used for all API error responses
(4xx and 5xx). Ensures consistent, structured, actionable error information
is returned to clients.

Also provides register_error_handlers(app) to install exception handlers on
a FastAPI app instance.

Requirements: 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.runtime.config import ConfigValidationError

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    """Classification buckets for API errors."""

    CONFIGURATION = "configuration"
    RUNTIME = "runtime"
    WORKFLOW = "workflow"
    CONNECTION = "connection"


@dataclass
class ErrorEnvelope:
    """Structured error response body returned on all 4xx/5xx responses.

    Fields:
        code: Uppercase snake_case error identifier (e.g. "INVALID_API_KEY").
        message: Human-readable description of the error.
        category: Error classification bucket.
        recoverable: Whether the operator can retry or fix this error.
        timestamp: ISO 8601 UTC timestamp of when the error occurred.
        suggestion: Optional remediation hint for the operator.
    """

    code: str
    message: str
    category: ErrorCategory
    recoverable: bool
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary.

        Returns:
            Dict with all fields; suggestion is omitted when None.
        """
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "category": self.category.value,
            "recoverable": self.recoverable,
            "timestamp": self.timestamp,
        }
        if self.suggestion is not None:
            result["suggestion"] = self.suggestion
        return result


def _category_from_status(status_code: int) -> ErrorCategory:
    """Determine the error category from an HTTP status code."""
    if status_code in (401, 403):
        return ErrorCategory.CONFIGURATION
    if status_code == 422:
        return ErrorCategory.CONFIGURATION
    if status_code >= 500:
        return ErrorCategory.RUNTIME
    return ErrorCategory.RUNTIME


def _code_from_status(status_code: int) -> str:
    """Map an HTTP status code to an error code string."""
    mapping = {
        400: "BAD_REQUEST",
        401: "AUTHENTICATION_FAILURE",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        502: "BAD_GATEWAY",
        503: "SERVICE_UNAVAILABLE",
    }
    return mapping.get(status_code, f"HTTP_{status_code}")


async def _handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle FastAPI HTTPException and map to ErrorEnvelope."""
    category = _category_from_status(exc.status_code)
    code = _code_from_status(exc.status_code)

    # Extract message from detail
    if isinstance(exc.detail, dict):
        message = exc.detail.get("detail", exc.detail.get("message", str(exc.detail)))
    elif isinstance(exc.detail, str):
        message = exc.detail
    else:
        message = str(exc.detail)

    suggestion = None
    if category == ErrorCategory.CONFIGURATION:
        if exc.status_code in (401, 403):
            suggestion = "Check your API token configuration. Ensure FORGE_API_TOKEN is set correctly."

    envelope = ErrorEnvelope(
        code=code,
        message=message,
        category=category,
        recoverable=exc.status_code < 500,
        suggestion=suggestion,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=envelope.to_dict(),
        headers=getattr(exc, "headers", None),
    )


async def _handle_config_validation_error(
    request: Request, exc: ConfigValidationError
) -> JSONResponse:
    """Handle ConfigValidationError and return 422 with field details."""
    envelope = ErrorEnvelope(
        code="VALIDATION_ERROR",
        message="Configuration validation failed",
        category=ErrorCategory.CONFIGURATION,
        recoverable=True,
        suggestion="Check the invalid fields and provide correct values.",
    )

    body = envelope.to_dict()
    body["fields"] = exc.errors

    return JSONResponse(
        status_code=422,
        content=body,
    )


async def _handle_unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Handle unhandled exceptions — log traceback, return generic envelope."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)

    envelope = ErrorEnvelope(
        code="INTERNAL_ERROR",
        message="An unexpected error occurred. Please try again later.",
        category=ErrorCategory.RUNTIME,
        recoverable=False,
    )

    return JSONResponse(
        status_code=500,
        content=envelope.to_dict(),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install all exception handlers on a FastAPI application.

    Args:
        app: The FastAPI application to register handlers on.
    """
    app.add_exception_handler(HTTPException, _handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(ConfigValidationError, _handle_config_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _handle_unhandled_exception)  # type: ignore[arg-type]
