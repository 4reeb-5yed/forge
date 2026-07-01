"""Event and decision models for the Forge Runtime event bus.

Defines the typed Event envelope, the EventType catalog, and the
DecisionRecord audit model that backs explainability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Closed catalog of all runtime event types.

    Each type has a documented payload schema. The event bus validates that
    every published event carries a type from this catalog.
    """

    # Model routing
    MODEL_SELECTED = "model.selected"
    MODEL_FALLBACK = "model.fallback"

    # Capability lifecycle
    CAPABILITY_REGISTERED = "capability.registered"
    CAPABILITY_DEREGISTERED = "capability.deregistered"
    CAPABILITY_DEGRADED = "capability.degraded"
    CAPABILITY_RECOVERED = "capability.recovered"

    # Workspace lifecycle
    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_DESTROYED = "workspace.destroyed"

    # Verification pipeline
    VERIFY_STAGE = "verify.stage"
    VERIFY_PASSED = "verify.passed"

    # Policy decisions
    POLICY_DECISION = "policy.decision"

    # Task lifecycle
    TASK_START = "task.start"
    TASK_DONE = "task.done"
    TASK_FAIL = "task.fail"

    # Build lifecycle
    BUILD_DONE = "build.done"

    # Specification and planning
    SPEC_READY = "spec.ready"
    TASKS_READY = "tasks.ready"

    # VCS operations
    COMMIT_DONE = "commit.done"
    COMMIT_FAILED = "commit.failed"

    # Approval gates
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_CANCELLED = "approval.cancelled"

    # Error events
    CONFIG_ERROR = "error.config"
    RUNTIME_ERROR = "error.runtime"
    WORKFLOW_ERROR = "error.workflow"

    # Documentation
    DOC_UPDATED = "doc_updated"
    DOC_DRIFT = "doc_drift"

    # Boot and readiness
    FORGE_BOOT_DISCOVERY_COMPLETE = "forge.boot.discovery_complete"
    FORGE_READY = "forge.ready"

    # Build timeout
    BUILD_TIMEOUT_STARTED = "build.timeout.started"
    BUILD_TIMEOUT_EXCEEDED = "build.timeout.exceeded"

    # Interaction
    QUESTION = "question"
    ERROR = "error"
    TOKEN = "token"

    # Budget
    BUDGET_EXCEEDED = "budget.exceeded"

    # Interrupt lifecycle
    INTERRUPT_PAUSED = "interrupt.paused"
    INTERRUPT_RESUMED = "interrupt.resumed"
    INTERRUPT_STOPPED = "interrupt.stopped"
    # Scheduler events
    SCHEDULER_STARTED = "scheduler.started"
    SCHEDULER_STOPPED = "scheduler.stopped"
    SCHEDULER_PAUSED = "scheduler.paused"
    SCHEDULER_RESUMED = "scheduler.resumed"
    SCHEDULER_SESSION_QUEUED = "scheduler.session.queued"
    SCHEDULER_SESSION_STARTED = "scheduler.session.started"
    SCHEDULER_SESSION_COMPLETED = "scheduler.session.completed"
    SCHEDULER_SESSION_FAILED = "scheduler.session.failed"
    SCHEDULER_SESSION_CANCELLED = "scheduler.session.cancelled"
    # Learning events
    LEARNING_ANALYSIS_COMPLETE = "learning.analysis.complete"
    LEARNING_RECOMMENDATION_GENERATED = "learning.recommendation.generated"
    INTERRUPT_REDIRECTED = "interrupt.redirected"


@dataclass(frozen=True)
class Event:
    """Typed event envelope — the fundamental unit of the event bus.

    Every runtime occurrence is published as an Event. The envelope carries
    ordering (seq), causality (causation_id), correlation (session_id),
    and a schema version for forward compatibility.

    Delivery semantics: at-least-once, ordered per session_id by seq.
    Subscribers are idempotent on (session_id, seq).
    """

    schema_version: int
    seq: int
    session_id: str
    type: EventType
    timestamp: datetime
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    correlation_id: str | None = None
    event_id: str = ""

    @staticmethod
    def create(
        *,
        type: EventType,
        session_id: str,
        source: str,
        payload: dict[str, Any] | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        event_id: str | None = None,
        seq: int = 0,
        schema_version: int = 1,
    ) -> Event:
        """Factory method that auto-fills timestamp and event_id if not provided."""
        import uuid
        return Event(
            schema_version=schema_version,
            seq=seq,
            session_id=session_id,
            type=type,
            timestamp=datetime.now(timezone.utc),
            source=source,
            payload=payload or {},
            causation_id=causation_id,
            correlation_id=correlation_id,
            event_id=event_id or str(uuid.uuid4()),
        )


class DecisionKind(str, Enum):
    """Kinds of non-trivial decisions recorded in the audit trail.

    Every decision the runtime makes that is a member of this set
    gets a DecisionRecord written to the Audit Trail.
    """

    RETRY = "retry"
    ESCALATE = "escalate"
    SKIP = "skip"
    MODEL_FALLBACK = "model_fallback"
    CAPABILITY_TRANSITION = "capability_transition"
    APPROVAL_GATE = "approval_gate"
    TASK_FAILED = "task_failed"
    INTENT_CLASSIFICATION = "intent_classification"
    CLARIFICATION = "clarification"
    MODEL_SELECTION = "model_selection"
    TOOL_SELECTION = "tool_selection"
    POLICY_APPLICATION = "policy_application"
    VERIFICATION_OUTCOME = "verification_outcome"
    TASK_OUTCOME = "task_outcome"


@dataclass(frozen=True)
class DecisionRecord:
    """Typed audit record capturing a non-trivial runtime decision.

    The rationale is constructed from inputs by the runtime (template-built),
    never by AI-generated prose. This makes it reproducible: identical inputs
    always produce an identical rationale.

    Backs the Runtime Inspector's explain_* methods.
    """

    kind: DecisionKind
    subject: str
    inputs: dict[str, Any] = field(default_factory=dict)
    decision: str = ""
    rationale: str = ""
    alternatives: list[str] = field(default_factory=list)
    causing_event_seq: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decision_id: str = ""
    session_id: str = ""
