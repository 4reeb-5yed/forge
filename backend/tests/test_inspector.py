"""Unit tests for RuntimeInspector — query-only facade.

Tests cover:
- Return current node, worker status, task queue, active task (Req 20.1)
- Return empty active task and empty queue when no build in progress (Req 20.2)
- Return most recent DecisionRecord for session (Req 20.3)
- Return not-found when no DecisionRecord or task exists (Req 20.4, 20.6)
- Reconstruct task history via causation links (Req 20.5)
- Derive all responses from runtime state, AuditTrail, Registry only (Req 20.7)
"""

from __future__ import annotations

import pytest

from app.runtime.audit import AuditTrail
from app.runtime.budget import SessionBudget
from app.runtime.events.models import (
    DecisionKind,
    DecisionRecord,
    Event,
    EventType,
)
from app.runtime.inspector import (
    DecisionNotFoundError,
    RuntimeInspector,
    RuntimeStatus,
    TaskExplanation,
    TaskNotFoundError,
    TaskView,
    WorkerStatus,
    _StateProvider,
)
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    Role,
    Task,
)
from app.runtime.registry import CapabilityRegistry


# --- Fixtures ---


@pytest.fixture
def audit_trail():
    return AuditTrail()


@pytest.fixture
def registry():
    return CapabilityRegistry(session_id="test")


@pytest.fixture
def state_provider():
    return _StateProvider()


@pytest.fixture
def inspector(audit_trail, registry, state_provider):
    return RuntimeInspector(
        audit_trail=audit_trail,
        registry=registry,
        state_provider=state_provider,
    )


def _make_task(task_id: str, title: str, status: str = "pending", **kwargs) -> Task:
    return Task(id=task_id, title=title, status=status, **kwargs)


# --- Test: get_status returns current node, worker status, task queue, active task ---


class TestGetStatusWithBuild:
    """Requirement 20.1: Return current node, worker status, task queue,
    active task from runtime state (within 2s)."""

    def test_returns_runtime_status_object(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running")],
            "current_task_id": "t1",
        })
        result = inspector.get_status("s1")
        assert isinstance(result, RuntimeStatus)

    def test_current_node_reflects_status(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [],
            "current_task_id": None,
        })
        result = inspector.get_status("s1")
        assert result.current_node == "execute"

    def test_worker_status_executing(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running")],
            "current_task_id": "t1",
        })
        result = inspector.get_status("s1")
        assert result.worker_status.status == "executing"
        assert result.worker_status.current_task_id == "t1"

    def test_task_queue_includes_pending_tasks(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [
                _make_task("t1", "Task 1", "running"),
                _make_task("t2", "Task 2", "pending"),
                _make_task("t3", "Task 3", "pending"),
            ],
            "current_task_id": "t1",
        })
        result = inspector.get_status("s1")
        assert len(result.task_queue) == 2
        assert result.task_queue[0].id == "t2"
        assert result.task_queue[1].id == "t3"

    def test_active_task_returned(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running")],
            "current_task_id": "t1",
        })
        result = inspector.get_status("s1")
        assert result.active_task is not None
        assert result.active_task.id == "t1"
        assert result.active_task.title == "Task 1"

    def test_budget_included_when_set(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [],
            "current_task_id": None,
        })
        budget = SessionBudget(token_limit=10000, session_id="s1")
        budget.charge(2000)
        state_provider.set_budget("s1", budget)
        result = inspector.get_status("s1")
        assert result.budget is not None
        assert result.budget["limit"] == 10000
        assert result.budget["consumed"] == 2000
        assert result.budget["remaining"] == 8000

    def test_verifying_worker_status(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "verifying",
            "tasks": [_make_task("t1", "Task 1", "verifying")],
            "current_task_id": "t1",
        })
        result = inspector.get_status("s1")
        assert result.worker_status.status == "verifying"
        assert result.current_node == "verify"


# --- Test: empty active task and empty queue when no build in progress ---


class TestGetStatusNoBuild:
    """Requirement 20.2: Return empty active task and empty queue
    when no build is in progress."""

    def test_idle_status_returns_empty_queue(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "idle",
            "tasks": [_make_task("t1", "Task 1", "pending")],
            "current_task_id": None,
        })
        result = inspector.get_status("s1")
        assert result.task_queue == []

    def test_idle_status_returns_none_active_task(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "idle",
            "tasks": [_make_task("t1", "Task 1", "pending")],
            "current_task_id": None,
        })
        result = inspector.get_status("s1")
        assert result.active_task is None

    def test_completed_status_returns_empty_queue(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "completed",
            "tasks": [_make_task("t1", "Task 1", "committed")],
            "current_task_id": None,
        })
        result = inspector.get_status("s1")
        assert result.task_queue == []
        assert result.active_task is None

    def test_no_state_defaults_to_idle(self, inspector):
        """Session with no state set returns idle defaults."""
        result = inspector.get_status("nonexistent-session")
        assert result.current_node == "idle"
        assert result.task_queue == []
        assert result.active_task is None

    def test_failed_status_returns_empty_queue(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "failed",
            "tasks": [_make_task("t1", "Task 1", "failed")],
            "current_task_id": None,
        })
        result = inspector.get_status("s1")
        assert result.task_queue == []
        assert result.active_task is None

    def test_stopped_status_returns_empty_queue(self, inspector, state_provider):
        """Stopped status also counts as no-build-in-progress (Req 20.2)."""
        state_provider.set_state("s1", {
            "status": "stopped",
            "tasks": [_make_task("t1", "Task 1", "pending")],
            "current_task_id": None,
        })
        result = inspector.get_status("s1")
        assert result.current_node == "idle"
        assert result.task_queue == []
        assert result.active_task is None


# --- Test: Return most recent DecisionRecord for session ---


class TestExplainLastDecision:
    """Requirement 20.3: Return the most recent DecisionRecord for the session,
    including kind, subject, inputs, decision, rationale, and alternatives."""

    def test_returns_most_recent_decision(self, inspector, audit_trail):
        audit_trail.write_decision(
            kind=DecisionKind.MODEL_SELECTION,
            subject="task-1",
            inputs={"role": "coder", "provider": "openrouter"},
            decision="selected openrouter/gpt-4",
            alternatives=["google/gemini"],
            session_id="s1",
        )
        audit_trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 2, "stage": "tests"},
            decision="retry",
            alternatives=["skip", "escalate"],
            session_id="s1",
        )
        result = inspector.explain_last_decision("s1")
        assert result.kind == DecisionKind.RETRY
        assert result.subject == "task-1"
        assert result.decision == "retry"
        assert "skip" in result.alternatives
        assert "escalate" in result.alternatives

    def test_decision_has_all_required_fields(self, inspector, audit_trail):
        audit_trail.write_decision(
            kind=DecisionKind.MODEL_FALLBACK,
            subject="model-routing",
            inputs={"from": "provider-a", "to": "provider-b", "reason": "timeout"},
            decision="fallback to provider-b",
            alternatives=["retry provider-a"],
            session_id="s1",
        )
        result = inspector.explain_last_decision("s1")
        assert isinstance(result, DecisionRecord)
        assert result.kind == DecisionKind.MODEL_FALLBACK
        assert result.subject == "model-routing"
        assert result.inputs == {"from": "provider-a", "to": "provider-b", "reason": "timeout"}
        assert result.decision == "fallback to provider-b"
        assert result.rationale != ""  # deterministic rationale built from inputs
        assert result.alternatives == ["retry provider-a"]


# --- Test: Return not-found when no DecisionRecord or task exists ---


class TestNotFoundResponses:
    """Requirements 20.4, 20.6: Return not-found when no DecisionRecord
    exists or task identifier does not exist."""

    def test_no_decision_raises_not_found(self, inspector):
        with pytest.raises(DecisionNotFoundError) as exc_info:
            inspector.explain_last_decision("s1")
        assert exc_info.value.session_id == "s1"

    def test_task_not_found_raises_error(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running")],
            "current_task_id": "t1",
        })
        with pytest.raises(TaskNotFoundError) as exc_info:
            inspector.explain_task("s1", "nonexistent-task")
        assert exc_info.value.task_id == "nonexistent-task"
        assert exc_info.value.session_id == "s1"

    def test_task_not_found_empty_task_list(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [],
            "current_task_id": None,
        })
        with pytest.raises(TaskNotFoundError):
            inspector.explain_task("s1", "any-task")

    def test_decision_not_found_different_session(self, inspector, audit_trail):
        # Write decision for session "s1"
        audit_trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 1},
            decision="retry",
            session_id="s1",
        )
        # Query session "s2" should not find it
        with pytest.raises(DecisionNotFoundError):
            inspector.explain_last_decision("s2")


# --- Test: Reconstruct task history via causation links ---


class TestExplainTask:
    """Requirement 20.5: Reconstruct task history via causation links
    (model selections, verifier outcomes, retries, commit)."""

    @pytest.mark.asyncio
    async def test_model_selections_collected(self, inspector, audit_trail, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running")],
            "current_task_id": "t1",
        })
        # Simulate a model.selected event for this task
        event = Event.create(
            type=EventType.MODEL_SELECTED,
            session_id="s1",
            source="model_router",
            payload={
                "task_id": "t1",
                "provider": "openrouter",
                "model": "gpt-4",
                "role": "coder",
                "attempt": 1,
            },
            correlation_id="s1",
            event_id="evt-1",
            seq=1,
        )
        await audit_trail.handle_event(event)

        result = inspector.explain_task("s1", "t1")
        assert isinstance(result, TaskExplanation)
        assert len(result.model_selections) == 1
        assert result.model_selections[0]["provider"] == "openrouter"
        assert result.model_selections[0]["model"] == "gpt-4"
        assert result.model_selections[0]["role"] == "coder"

    @pytest.mark.asyncio
    async def test_verifier_outcomes_collected(self, inspector, audit_trail, state_provider):
        state_provider.set_state("s1", {
            "status": "verifying",
            "tasks": [_make_task("t1", "Task 1", "verifying")],
            "current_task_id": "t1",
        })
        event = Event.create(
            type=EventType.VERIFY_STAGE,
            session_id="s1",
            source="verification_pipeline",
            payload={
                "task_id": "t1",
                "stage_name": "tests",
                "status": "passed",
                "detail": "All 12 tests passed",
            },
            correlation_id="s1",
            event_id="evt-2",
            seq=2,
        )
        await audit_trail.handle_event(event)

        result = inspector.explain_task("s1", "t1")
        assert len(result.verifier_outcomes) == 1
        assert result.verifier_outcomes[0]["stage"] == "tests"
        assert result.verifier_outcomes[0]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_retries_from_decisions(self, inspector, audit_trail, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running", attempts=2)],
            "current_task_id": "t1",
        })
        audit_trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="t1",
            inputs={"attempt": 2, "stage": "tests", "reason": "test failure"},
            decision="retry",
            alternatives=["skip", "escalate"],
            session_id="s1",
        )

        result = inspector.explain_task("s1", "t1")
        assert len(result.retries) == 1
        assert result.retries[0]["kind"] == "retry"
        assert result.retries[0]["decision"] == "retry"
        assert "skip" in result.retries[0]["alternatives"]

    @pytest.mark.asyncio
    async def test_commit_info_collected(self, inspector, audit_trail, state_provider):
        state_provider.set_state("s1", {
            "status": "committing",
            "tasks": [_make_task("t1", "Task 1", "committed")],
            "current_task_id": "t1",
        })
        event = Event.create(
            type=EventType.COMMIT_DONE,
            session_id="s1",
            source="workflow_engine",
            payload={
                "task_id": "t1",
                "sha": "abc123def",
                "files": ["src/main.py", "tests/test_main.py"],
            },
            correlation_id="s1",
            event_id="evt-3",
            seq=3,
        )
        await audit_trail.handle_event(event)

        result = inspector.explain_task("s1", "t1")
        assert result.commit is not None
        assert result.commit["sha"] == "abc123def"
        assert "src/main.py" in result.commit["files"]

    @pytest.mark.asyncio
    async def test_full_task_history_assembled(
        self, inspector, audit_trail, state_provider
    ):
        """Full integration: model selection + verify + retry + commit."""
        state_provider.set_state("s1", {
            "status": "committing",
            "tasks": [_make_task("t1", "Build feature", "committed", attempts=2)],
            "current_task_id": "t1",
        })

        # Model selection event
        await audit_trail.handle_event(Event.create(
            type=EventType.MODEL_SELECTED,
            session_id="s1",
            source="model_router",
            payload={"task_id": "t1", "provider": "openrouter", "model": "gpt-4", "role": "coder", "attempt": 1},
            correlation_id="s1",
            event_id="evt-1",
            seq=1,
        ))

        # Verify stage failed
        await audit_trail.handle_event(Event.create(
            type=EventType.VERIFY_STAGE,
            session_id="s1",
            source="verification",
            payload={"task_id": "t1", "stage_name": "tests", "status": "failed", "detail": "2 tests failed"},
            correlation_id="s1",
            event_id="evt-2",
            seq=2,
        ))

        # Policy decision: retry
        await audit_trail.handle_event(Event.create(
            type=EventType.POLICY_DECISION,
            session_id="s1",
            source="policy_engine",
            payload={"task_id": "t1", "decision": "retry", "rule": "retry_on_test_fail", "subject": "t1"},
            correlation_id="s1",
            event_id="evt-3",
            seq=3,
            causation_id="evt-2",
        ))

        # Retry decision record
        audit_trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="t1",
            inputs={"attempt": 1, "stage": "tests"},
            decision="retry",
            alternatives=["skip"],
            session_id="s1",
        )

        # Verify passed on retry
        await audit_trail.handle_event(Event.create(
            type=EventType.VERIFY_PASSED,
            session_id="s1",
            source="verification",
            payload={"task_id": "t1"},
            correlation_id="s1",
            event_id="evt-4",
            seq=4,
        ))

        # Commit done
        await audit_trail.handle_event(Event.create(
            type=EventType.COMMIT_DONE,
            session_id="s1",
            source="workflow_engine",
            payload={"task_id": "t1", "sha": "deadbeef", "files": ["src/feature.py"]},
            correlation_id="s1",
            event_id="evt-5",
            seq=5,
            causation_id="evt-4",
        ))

        result = inspector.explain_task("s1", "t1")
        assert result.task_id == "t1"
        assert len(result.model_selections) == 1
        assert len(result.verifier_outcomes) == 2  # failed + passed
        assert len(result.retries) == 1
        assert result.commit is not None
        assert result.commit["sha"] == "deadbeef"
        assert len(result.policy_decisions) == 1

    @pytest.mark.asyncio
    async def test_model_fallback_events_collected(
        self, inspector, audit_trail, state_provider
    ):
        """Model fallback events appear in explain_task with fallback=True (Req 20.5)."""
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Task 1", "running")],
            "current_task_id": "t1",
        })
        # Primary model selected
        await audit_trail.handle_event(Event.create(
            type=EventType.MODEL_SELECTED,
            session_id="s1",
            source="model_router",
            payload={
                "task_id": "t1",
                "provider": "openrouter",
                "model": "gpt-4",
                "role": "coder",
                "attempt": 1,
            },
            correlation_id="s1",
            event_id="evt-1",
            seq=1,
        ))
        # Fallback triggered
        await audit_trail.handle_event(Event.create(
            type=EventType.MODEL_FALLBACK,
            session_id="s1",
            source="model_router",
            payload={
                "task_id": "t1",
                "provider": "google",
                "model": "gemini-pro",
                "role": "coder",
                "attempt": 2,
                "reason": "timeout",
            },
            correlation_id="s1",
            event_id="evt-2",
            seq=2,
            causation_id="evt-1",
        ))

        result = inspector.explain_task("s1", "t1")
        assert len(result.model_selections) == 2
        # First is normal selection
        assert result.model_selections[0].get("fallback") is None
        assert result.model_selections[0]["provider"] == "openrouter"
        # Second is the fallback
        assert result.model_selections[1]["fallback"] is True
        assert result.model_selections[1]["provider"] == "google"
        assert result.model_selections[1]["model"] == "gemini-pro"
        assert result.model_selections[1]["reason"] == "timeout"


# --- Test: Derive all responses from runtime state, AuditTrail, Registry only ---


class TestNoAIInvocation:
    """Requirement 20.7: Derive all responses from runtime state, AuditTrail,
    and CapabilityRegistry only (no AI)."""

    def test_capability_summary_from_registry(self, inspector):
        """capability_summary() reads from the registry, not AI."""
        summary = inspector.capability_summary()
        assert isinstance(summary, dict)
        assert "available" in summary
        assert "degraded" in summary
        assert "mode" in summary

    @pytest.mark.asyncio
    async def test_capability_summary_reflects_registry_state(
        self, inspector, registry
    ):
        """After registering a capability, summary reflects it."""
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
            provider_name="openrouter",
        )
        await registry.register(entry)
        summary = inspector.capability_summary()
        assert "ai_coder" in summary["available"]
        assert summary["available"]["ai_coder"] == "healthy"

    def test_get_status_no_ai_call(self, inspector, state_provider):
        """get_status is purely state-derived, never calls AI."""
        state_provider.set_state("s1", {
            "status": "idle",
            "tasks": [],
            "current_task_id": None,
        })
        # This should complete synchronously with no external calls
        result = inspector.get_status("s1")
        assert result.current_node == "idle"

    def test_explain_last_decision_no_ai(self, inspector, audit_trail):
        """explain_last_decision reads from audit trail only."""
        audit_trail.write_decision(
            kind=DecisionKind.TOOL_SELECTION,
            subject="task-1",
            inputs={"tool": "aider", "reason": "primary"},
            decision="use aider",
            session_id="s1",
        )
        result = inspector.explain_last_decision("s1")
        # The rationale is template-built from inputs, never AI-generated
        assert "aider" in result.rationale or "tool" in result.rationale


# --- Test: Individual accessor methods ---


class TestIndividualAccessors:
    """Test the individual current_node, worker_status, task_queue,
    active_task accessor methods."""

    def test_current_node_planning(self, inspector, state_provider):
        state_provider.set_state("s1", {"status": "planning", "tasks": [], "current_task_id": None})
        assert inspector.current_node("s1") == "plan"

    def test_current_node_clarifying(self, inspector, state_provider):
        state_provider.set_state("s1", {"status": "clarifying", "tasks": [], "current_task_id": None})
        assert inspector.current_node("s1") == "clarify"

    def test_worker_status_idle(self, inspector, state_provider):
        state_provider.set_state("s1", {"status": "idle", "tasks": [], "current_task_id": None})
        ws = inspector.worker_status("s1")
        assert ws.status == "idle"
        assert ws.current_task_id is None

    def test_task_queue_no_build(self, inspector, state_provider):
        state_provider.set_state("s1", {"status": "idle", "tasks": [_make_task("t1", "T", "pending")], "current_task_id": None})
        assert inspector.task_queue("s1") == []

    def test_task_queue_with_build(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [
                _make_task("t1", "Task 1", "running"),
                _make_task("t2", "Task 2", "pending"),
            ],
            "current_task_id": "t1",
        })
        queue = inspector.task_queue("s1")
        assert len(queue) == 1
        assert queue[0].id == "t2"

    def test_active_task_no_build(self, inspector, state_provider):
        state_provider.set_state("s1", {"status": "idle", "tasks": [], "current_task_id": None})
        assert inspector.active_task("s1") is None

    def test_active_task_with_build(self, inspector, state_provider):
        state_provider.set_state("s1", {
            "status": "executing",
            "tasks": [_make_task("t1", "Active Task", "running")],
            "current_task_id": "t1",
        })
        task = inspector.active_task("s1")
        assert task is not None
        assert task.id == "t1"
        assert task.title == "Active Task"


# --- Test: StateProvider ---


class TestStateProvider:
    """Test the _StateProvider helper used by the inspector."""

    def test_default_state_returned_for_unknown_session(self):
        provider = _StateProvider()
        state = provider.get_state("unknown")
        assert state["status"] == "idle"
        assert state["tasks"] == []
        assert state["current_task_id"] is None

    def test_set_and_get_state(self):
        provider = _StateProvider()
        provider.set_state("s1", {"status": "executing", "tasks": [], "current_task_id": "t1"})
        state = provider.get_state("s1")
        assert state["status"] == "executing"

    def test_budget_summary_none_when_not_set(self):
        provider = _StateProvider()
        assert provider.get_budget_summary("s1") is None

    def test_budget_summary_returned_when_set(self):
        provider = _StateProvider()
        budget = SessionBudget(token_limit=5000, session_id="s1")
        budget.charge(1000)
        provider.set_budget("s1", budget)
        summary = provider.get_budget_summary("s1")
        assert summary == {"limit": 5000, "consumed": 1000, "remaining": 4000}
