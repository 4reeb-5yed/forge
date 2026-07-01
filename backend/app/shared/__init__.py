"""Shared types and exceptions used across layers.

This module contains types that need to be shared between layers (e.g., Adapters
and Runtime) without creating layer boundary violations. Each layer imports from
this shared module instead of importing from other layers.

Types here are intentionally minimal and stable — they define interfaces, not logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class HealthStatus(str, Enum):
    """Status of a capability health check."""

    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True)
class Health:
    """Result of a capability health check.

    Every protocol's health_check() returns this type, providing a uniform
    way to assess capability readiness.
    """

    ok: bool
    status: HealthStatus = HealthStatus.OK
    latency_ms: float | None = None
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @staticmethod
    def healthy(latency_ms: float | None = None) -> Health:
        return Health(ok=True, status=HealthStatus.OK, latency_ms=latency_ms)

    @staticmethod
    def unhealthy(message: str, latency_ms: float | None = None) -> Health:
        return Health(
            ok=False, status=HealthStatus.UNHEALTHY, latency_ms=latency_ms, message=message
        )

    @staticmethod
    def degraded(message: str, latency_ms: float | None = None) -> Health:
        return Health(
            ok=False, status=HealthStatus.DEGRADED, latency_ms=latency_ms, message=message
        )


@dataclass(frozen=True)
class ToolResult:
    """Result of a CodingTool execution.

    Captures whether the tool succeeded, any files it modified, and output/error details.
    """

    success: bool
    files_modified: list[str] = field(default_factory=list)
    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PermanentError(Exception):
    """Raised when a provider call fails with a permanent (non-retryable) error.

    Permanent errors include authentication failures, authorization failures,
    malformed requests, and unsupported model errors. These should NOT be retried.
    """

    def __init__(self, message: str, *, provider: str = "", error_type: str = "") -> None:
        self.provider = provider
        self.error_type = error_type
        super().__init__(message)
