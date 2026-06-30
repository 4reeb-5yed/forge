"""Shared types used across the Forge Runtime plugin protocols and core components."""

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
    """Result of a plugin health check.

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


class VerifyStatus(str, Enum):
    """Outcome status of a single verification stage."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class VerifyResult:
    """Result of a single verification stage (Verifier.verify()).

    Used by the Verification Pipeline to track per-stage outcomes.
    """

    stage_name: str
    status: VerifyStatus
    blocking: bool
    detail: str = ""
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifyContext:
    """Context passed to a Verifier, scoping what should be verified.

    Provides the verifier with everything it needs to evaluate task output.
    """

    session_id: str
    task_id: str
    workspace_path: str
    files_modified: list[str] = field(default_factory=list)
    base_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DocUpdateResult:
    """Result of a DocWriter.update_docs() call.

    Captures which documentation files were created or modified.
    """

    success: bool
    files_updated: list[str] = field(default_factory=list)
    files_created: list[str] = field(default_factory=list)
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TwinDiff:
    """A diff computed from the Digital Twin representing changes since last doc sync.

    DocWriter consumes this to know what documentation needs updating.
    """

    files_added: list[str] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
