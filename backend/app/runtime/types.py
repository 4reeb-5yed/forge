"""Shared types used across the Forge Runtime plugin protocols and core components.

This module re-exports types from app.shared for backward compatibility.
The canonical source for Health, ToolResult, and PermanentError is app.shared.
"""

from __future__ import annotations

# Re-export shared types from app.shared
# This maintains backward compatibility for code that imports from app.runtime.types
from app.shared import (
    Health,
    HealthStatus,
    ToolResult,
)

# Also re-export PermanentError for convenience
from app.shared import PermanentError

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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

    @property
    def has_changes(self) -> bool:
        """True if this diff describes any changes."""
        return bool(self.files_added or self.files_removed or self.files_modified)
