"""Learning Recorder — records build outcomes for later analysis.

Records one outcome entry per executed task on build finalize. Surfaces
recommendations but never auto-applies them.

Requirements: 25.1, 25.2, 25.3, 25.4, 25.5
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from app.runtime.events.models import Event, EventType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class OutcomeStatus(str, Enum):
    """Outcome status for a task — exactly one of success or failure (Req 25.2)."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass
class OutcomeEntry:
    """A single task outcome entry (Req 25.1).

    Attributes:
        task_id: Identifier of the task.
        task_type: The type/category of the task.
        tool: The coding tool used for the task.
        model: The AI model used.
        role: The role that drove the task.
        outcome_status: Exactly 'success' or 'failure' (Req 25.2).
        retry_count: Non-negative integer of retries.
        escalation_flag: Whether the task was escalated.
        session_id: The session this outcome belongs to.
        timestamp: When the outcome was recorded.
    """

    task_id: str
    task_type: str
    tool: str
    model: str
    role: str
    outcome_status: OutcomeStatus
    retry_count: int
    escalation_flag: bool
    session_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        """Validate invariants."""
        if self.retry_count < 0:
            raise ValueError(
                f"retry_count must be non-negative, got {self.retry_count}"
            )
        if not isinstance(self.outcome_status, OutcomeStatus):
            raise ValueError(
                f"outcome_status must be OutcomeStatus, got {self.outcome_status!r}"
            )


@dataclass
class Recommendation:
    """A recommendation derived from recorded outcomes.

    Recommendations are advisory only — never auto-applied (Req 25.5).
    """

    category: str
    description: str
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class EventEmitter(Protocol):
    """Protocol for emitting events (decoupled from EventBus)."""

    async def publish(self, event: Event) -> Any: ...


# ---------------------------------------------------------------------------
# Learning Recorder
# ---------------------------------------------------------------------------


class LearningRecorder:
    """Records build outcomes for later analysis.

    Records one outcome entry per executed task on build finalize.
    Persists entries to the relational store (in-memory for v1).
    On persistence failure, completes the build without discarding the result
    and emits a failure event.

    Never applies recommendations automatically (Req 25.5).

    Requirements:
        25.1 — Record one outcome entry per executed task on build finalize
        25.2 — Record outcome status as exactly success or failure
        25.3 — Complete build on recording failure; emit failure event
        25.4 — Return recommendations derived from recorded outcomes
        25.5 — Never apply recommendations automatically
    """

    def __init__(self, event_emitter: EventEmitter | None = None) -> None:
        """Initialize the LearningRecorder.

        Args:
            event_emitter: Object with an async publish(event) method for
                observability (e.g., EventBus). If None, failure events are logged only.
        """
        self._event_emitter = event_emitter
        # In-memory relational store: session_id -> list of outcomes
        self._store: dict[str, list[OutcomeEntry]] = defaultdict(list)

    def record_outcome(self, entry: OutcomeEntry) -> None:
        """Persist a single outcome entry to the in-memory relational store.

        Args:
            entry: The outcome entry to persist.
        """
        self._store[entry.session_id].append(entry)

    async def record_build_outcomes(
        self,
        session_id: str,
        task_results: list[dict[str, Any]],
    ) -> None:
        """Record one outcome entry per executed task at build finalize (Req 25.1).

        On recording failure for any individual task, emits an error event
        but does NOT discard the build result (Req 25.3).

        Args:
            session_id: The build session identifier.
            task_results: List of dicts with keys: task_id, task_type, tool,
                model, role, outcome_status, retry_count, escalation_flag.
        """
        for task_result in task_results:
            try:
                entry = OutcomeEntry(
                    task_id=task_result["task_id"],
                    task_type=task_result.get("task_type", "unknown"),
                    tool=task_result.get("tool", "unknown"),
                    model=task_result.get("model", "unknown"),
                    role=task_result.get("role", "unknown"),
                    outcome_status=OutcomeStatus(
                        task_result.get("outcome_status", "failure")
                    ),
                    retry_count=int(task_result.get("retry_count", 0)),
                    escalation_flag=bool(task_result.get("escalation_flag", False)),
                    session_id=session_id,
                )
                self.record_outcome(entry)
            except Exception as exc:
                # Req 25.3: complete the build without discarding result;
                # emit failure event
                logger.warning(
                    "Failed to record outcome for task '%s': %s",
                    task_result.get("task_id", "unknown"),
                    exc,
                )
                await self._emit_recording_failure(
                    session_id=session_id,
                    task_id=task_result.get("task_id", "unknown"),
                    error=str(exc),
                )

    def get_outcomes(self, session_id: str) -> list[OutcomeEntry]:
        """Retrieve all recorded outcomes for a session.

        Args:
            session_id: The session to query.

        Returns:
            List of outcome entries for the session (empty if none).
        """
        return list(self._store.get(session_id, []))

    def get_recommendations(self, session_id: str | None = None) -> list[Recommendation]:
        """Return recommendations derived from recorded outcomes (Req 25.4).

        Simple heuristics:
        - Frequent failures on a tool/model suggest switching.
        - High retry counts suggest configuration tuning.

        Recommendations are NEVER auto-applied (Req 25.5).

        Args:
            session_id: If provided, scope recommendations to that session.
                If None, analyze all recorded outcomes.

        Returns:
            List of advisory recommendations.
        """
        if session_id is not None:
            entries = self._store.get(session_id, [])
        else:
            entries = [
                e for session_entries in self._store.values() for e in session_entries
            ]

        if not entries:
            return []

        recommendations: list[Recommendation] = []

        # Heuristic 1: Frequent failures on a specific tool suggest switching
        tool_failures: dict[str, int] = defaultdict(int)
        tool_totals: dict[str, int] = defaultdict(int)
        for entry in entries:
            tool_totals[entry.tool] += 1
            if entry.outcome_status == OutcomeStatus.FAILURE:
                tool_failures[entry.tool] += 1

        for tool, failure_count in tool_failures.items():
            total = tool_totals[tool]
            if failure_count >= 2 and failure_count / total > 0.5:
                recommendations.append(
                    Recommendation(
                        category="tool_switch",
                        description=(
                            f"Tool '{tool}' has a high failure rate "
                            f"({failure_count}/{total}). Consider switching tools."
                        ),
                        evidence={
                            "tool": tool,
                            "failures": failure_count,
                            "total": total,
                        },
                    )
                )

        # Heuristic 2: Frequent failures on a specific model suggest switching
        model_failures: dict[str, int] = defaultdict(int)
        model_totals: dict[str, int] = defaultdict(int)
        for entry in entries:
            model_totals[entry.model] += 1
            if entry.outcome_status == OutcomeStatus.FAILURE:
                model_failures[entry.model] += 1

        for model, failure_count in model_failures.items():
            total = model_totals[model]
            if failure_count >= 2 and failure_count / total > 0.5:
                recommendations.append(
                    Recommendation(
                        category="model_switch",
                        description=(
                            f"Model '{model}' has a high failure rate "
                            f"({failure_count}/{total}). Consider switching models."
                        ),
                        evidence={
                            "model": model,
                            "failures": failure_count,
                            "total": total,
                        },
                    )
                )

        # Heuristic 3: High retry counts suggest configuration tuning
        high_retry_entries = [e for e in entries if e.retry_count >= 3]
        if high_retry_entries:
            avg_retries = sum(e.retry_count for e in high_retry_entries) / len(
                high_retry_entries
            )
            affected_tools = sorted({e.tool for e in high_retry_entries})
            recommendations.append(
                Recommendation(
                    category="retry_tuning",
                    description=(
                        f"{len(high_retry_entries)} task(s) required 3+ retries "
                        f"(avg {avg_retries:.1f}). Consider tuning retry configuration."
                    ),
                    evidence={
                        "count": len(high_retry_entries),
                        "avg_retries": avg_retries,
                        "affected_tools": affected_tools,
                    },
                )
            )

        return recommendations

    async def _emit_recording_failure(
        self, session_id: str, task_id: str, error: str
    ) -> None:
        """Emit an error event indicating recording failure (Req 25.3)."""
        if self._event_emitter is None:
            return

        event = Event.create(
            type=EventType.ERROR,
            session_id=session_id,
            source="learning_recorder",
            correlation_id=session_id,
            payload={
                "error": "outcome_recording_failure",
                "task_id": task_id,
                "detail": error,
            },
            correlation_id=session_id,
            event_id=str(uuid.uuid4()),
        )
        try:
            await self._event_emitter.publish(event)
        except Exception:
            # If we can't even emit the error event, silently continue
            # to preserve the build result (Req 25.3).
            logger.error(
                "Failed to emit recording failure event for task '%s'",
                task_id,
            )
