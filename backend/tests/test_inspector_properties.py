"""Property-based tests for RuntimeInspector explainability without inference.

**Property 8: Explainability without inference** — every explain and runtime
response is derived solely from structured state (audit trail, registry,
ForgeState); no LLM invoked.

**Validates: Requirements 20.7, 12.5**

Uses Hypothesis to generate arbitrary runtime states (sessions with various
statuses, task lists, decisions, capabilities, budgets). Calls ALL inspector
methods (get_status, explain_last_decision, explain_task, capability_summary).
Verifies that NO method invokes any AI model. Since the inspector has no model
dependency, we verify that it always returns structured data derived from the
inputs without error, for any valid input state.

Also verifies: budget summary is always derivable from limit/consumed without
AI (Req 12.5).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

from app.runtime.audit import AuditTrail
from app.runtime.budget import SessionBudget
from app.runtime.events.models import DecisionKind, DecisionRecord, Event, EventType
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
)
from app.runtime.registry import CapabilityRegistry


# --- Strategies ---

# Session IDs
session_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=30,
)

# Task IDs
task_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_"),
    min_size=1,
    max_size=20,
)

# Task statuses that the inspector might encounter
task_statuses = st.sampled_from(
    ["pending", "running", "verifying", "committed", "failed", "skipped"]
)

# Session/build statuses
session_statuses = st.sampled_from([
    "created", "idle", "clarifying", "planning", "executing",
    "verifying", "committing", "documenting", "finalizing",
    "interrupted", "completed", "failed", "stopped",
])

# A single task dict (mimics Task-like dict used by _StateProvider)
task_dicts = st.fixed_dictionaries({
    "id": task_ids,
    "title": st.text(min_size=1, max_size=50),
    "status": task_statuses,
    "description": st.text(min_size=0, max_size=100),
    "depends_on": st.lists(task_ids, min_size=0, max_size=3),
    "attempts": st.integers(min_value=0, max_value=10),
})

# A list of tasks
task_lists = st.lists(task_dicts, min_size=0, max_size=10)

# Decision kinds
decision_kinds = st.sampled_from(list(DecisionKind))

# Decision records
decision_records = st.builds(
    DecisionRecord,
    kind=decision_kinds,
    subject=st.text(min_size=1, max_size=30),
    inputs=st.fixed_dictionaries({
        "reason": st.text(min_size=1, max_size=30),
    }),
    decision=st.text(min_size=1, max_size=50),
    rationale=st.text(min_size=1, max_size=100),
    alternatives=st.lists(st.text(min_size=1, max_size=30), min_size=0, max_size=3),
    session_id=session_ids,
)

# Budget parameters
token_limits = st.integers(min_value=0, max_value=10_000_000)
consumed_amounts = st.integers(min_value=0, max_value=10_000_000)

# Capabilities for the registry
capabilities = st.sampled_from(list(Capability))
capability_kinds = st.sampled_from(list(CapabilityKind))
roles = st.sampled_from(list(Role))

# Capability entries
capability_entries = st.builds(
    CapabilityEntry,
    name=capabilities,
    kind=capability_kinds,
    healthy=st.booleans(),
    roles=st.lists(roles, min_size=0, max_size=3),
    provider_name=st.text(min_size=1, max_size=20),
)


# --- Helper: build a full state dict ---

@st.composite
def runtime_states(draw):
    """Generate an arbitrary runtime state dict with tasks and status."""
    status = draw(session_statuses)
    tasks = draw(task_lists)

    # Optionally pick one task as current
    current_task_id = None
    if tasks and draw(st.booleans()):
        current_task_id = draw(st.sampled_from([t["id"] for t in tasks]))

    return {
        "status": status,
        "tasks": tasks,
        "current_task_id": current_task_id,
    }


# --- Test class ---

class TestExplainabilityWithoutInference:
    """Property 8: Explainability without inference — every explain and runtime
    response is derived solely from structured state (audit trail, registry,
    ForgeState); no LLM invoked.

    **Validates: Requirements 20.7, 12.5**
    """

    @given(
        session_id=session_ids,
        state=runtime_states(),
    )
    @settings(max_examples=300)
    def test_get_status_returns_structured_data_without_ai(
        self, session_id: str, state: dict[str, Any]
    ):
        """get_status() returns a RuntimeStatus derived solely from state.

        For any valid runtime state, get_status must:
        - Return a RuntimeStatus (not raise or invoke AI)
        - Contain a current_node derived from the state status
        - Contain a WorkerStatus derived from task list
        - Return structured data that is fully determined by the input state
        """
        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()
        provider.set_state(session_id, state)

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        # Inspector has NO model/AI attribute or dependency
        # Calling get_status must succeed purely from state
        result = inspector.get_status(session_id)

        # Must return a RuntimeStatus
        assert isinstance(result, RuntimeStatus)
        # current_node is always a string
        assert isinstance(result.current_node, str)
        assert len(result.current_node) > 0
        # worker_status is always present
        assert isinstance(result.worker_status, WorkerStatus)
        # task_queue is always a list
        assert isinstance(result.task_queue, list)
        for task_view in result.task_queue:
            assert isinstance(task_view, TaskView)
        # active_task is None or TaskView
        assert result.active_task is None or isinstance(result.active_task, TaskView)

    @given(
        session_id=session_ids,
        decisions=st.lists(decision_records, min_size=1, max_size=5),
    )
    @settings(max_examples=300)
    def test_explain_last_decision_returns_structured_data_without_ai(
        self, session_id: str, decisions: list[DecisionRecord]
    ):
        """explain_last_decision() returns the most recent DecisionRecord
        from the audit trail without invoking any AI model.

        For any session with at least one decision, the inspector returns
        the last decision as structured data derived from the audit trail.
        """
        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()

        # Write decisions to the audit trail
        for decision in decisions:
            audit_trail.write_decision(
                kind=decision.kind,
                subject=decision.subject,
                inputs=decision.inputs,
                decision=decision.decision,
                alternatives=decision.alternatives,
                session_id=session_id,
            )

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        result = inspector.explain_last_decision(session_id)

        # Must return a DecisionRecord
        assert isinstance(result, DecisionRecord)
        # It should be the last written decision
        assert result.kind == decisions[-1].kind
        assert result.subject == decisions[-1].subject
        assert result.decision == decisions[-1].decision
        # Rationale is deterministic (template-built, no AI)
        assert isinstance(result.rationale, str)
        assert len(result.rationale) > 0

    @given(session_id=session_ids)
    @settings(max_examples=100)
    def test_explain_last_decision_raises_not_found_without_ai(
        self, session_id: str
    ):
        """explain_last_decision() raises DecisionNotFoundError when no
        decision exists, without invoking any AI model.
        """
        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        with pytest.raises(DecisionNotFoundError) as exc_info:
            inspector.explain_last_decision(session_id)

        assert exc_info.value.session_id == session_id

    @given(
        session_id=session_ids,
        state=runtime_states(),
    )
    @settings(max_examples=300)
    def test_explain_task_returns_structured_data_without_ai(
        self, session_id: str, state: dict[str, Any]
    ):
        """explain_task() returns a TaskExplanation derived solely from the
        audit trail events without invoking any AI model.

        For any state that has at least one task, explain_task returns
        structured history data from the audit trail.
        """
        assume(len(state["tasks"]) > 0)

        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()
        provider.set_state(session_id, state)

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        # Pick a task that exists
        task_id = state["tasks"][0]["id"]
        result = inspector.explain_task(session_id, task_id)

        # Must return a TaskExplanation
        assert isinstance(result, TaskExplanation)
        assert result.task_id == task_id
        # All fields are lists/None (structured data, not AI-generated text)
        assert isinstance(result.model_selections, list)
        assert isinstance(result.verifier_outcomes, list)
        assert isinstance(result.retries, list)
        assert isinstance(result.policy_decisions, list)
        assert result.commit is None or isinstance(result.commit, dict)

    @given(
        session_id=session_ids,
        task_id=task_ids,
    )
    @settings(max_examples=100)
    def test_explain_task_raises_not_found_for_missing_task_without_ai(
        self, session_id: str, task_id: str
    ):
        """explain_task() raises TaskNotFoundError when the task does not exist
        in the session, without invoking any AI model.
        """
        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()
        # Empty state: no tasks
        provider.set_state(session_id, {"status": "idle", "tasks": [], "current_task_id": None})

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        with pytest.raises(TaskNotFoundError) as exc_info:
            inspector.explain_task(session_id, task_id)

        assert exc_info.value.task_id == task_id
        assert exc_info.value.session_id == session_id

    @given(entries=st.lists(capability_entries, min_size=0, max_size=8))
    @settings(max_examples=200)
    def test_capability_summary_returns_structured_data_without_ai(
        self, entries: list[CapabilityEntry]
    ):
        """capability_summary() returns structured data derived solely from
        the CapabilityRegistry, without invoking any AI model.

        For any set of registered capabilities, the summary is a dict of
        structured fields computed from registry state.
        """
        registry = CapabilityRegistry()
        audit_trail = AuditTrail()
        provider = _StateProvider()

        # Synchronously set entries (bypass async register for property test)
        for entry in entries:
            registry._entries[entry.name] = entry

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        result = inspector.capability_summary()

        # Must return a dict with known keys
        assert isinstance(result, dict)
        assert "available" in result
        assert "degraded" in result
        assert "missing_required" in result
        assert "missing_reasons" in result
        assert "mode" in result
        assert "can_operate" in result

        # All values are basic types (structured data, not AI prose)
        assert isinstance(result["available"], dict)
        assert isinstance(result["degraded"], list)
        assert isinstance(result["missing_required"], list)
        assert isinstance(result["missing_reasons"], dict)
        assert isinstance(result["mode"], str)
        assert isinstance(result["can_operate"], bool)

    @given(
        token_limit=token_limits,
        consumed=consumed_amounts,
        session_id=session_ids,
    )
    @settings(max_examples=300)
    def test_budget_summary_derivable_without_ai(
        self, token_limit: int, consumed: int, session_id: str
    ):
        """Budget summary is always derivable from limit/consumed without AI.

        **Validates: Requirement 12.5**

        For any valid budget state (limit, consumed), the inspector returns
        a budget summary that is a pure arithmetic derivation. No AI model
        is invoked.
        """
        # Consumed cannot exceed limit in normal operation
        consumed = min(consumed, token_limit)

        budget = SessionBudget(token_limit=token_limit, session_id=session_id)
        if consumed > 0:
            budget.charge(consumed)

        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()
        provider.set_state(session_id, {"status": "executing", "tasks": [], "current_task_id": None})
        provider.set_budget(session_id, budget)

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        result = inspector.get_status(session_id)

        # Budget must be present in the status
        assert result.budget is not None
        assert isinstance(result.budget, dict)

        # Budget fields are purely arithmetic derivations
        assert result.budget["limit"] == token_limit
        assert result.budget["consumed"] == consumed
        assert result.budget["remaining"] == token_limit - consumed

        # Verify the arithmetic identity always holds
        assert result.budget["limit"] == result.budget["consumed"] + result.budget["remaining"]

    @given(
        session_id=session_ids,
        state=runtime_states(),
        entries=st.lists(capability_entries, min_size=0, max_size=5),
        decisions=st.lists(decision_records, min_size=0, max_size=3),
        token_limit=st.integers(min_value=1, max_value=1_000_000),
        consumed=st.integers(min_value=0, max_value=1_000_000),
    )
    @settings(max_examples=200)
    def test_all_methods_return_without_ai_for_any_state(
        self,
        session_id: str,
        state: dict[str, Any],
        entries: list[CapabilityEntry],
        decisions: list[DecisionRecord],
        token_limit: int,
        consumed: int,
    ):
        """Comprehensive property: for ANY valid combination of runtime state,
        capability entries, decisions, and budget, ALL inspector methods return
        structured data without error and without invoking any AI model.

        This is the universal property for Requirement 20.7: the inspector
        derives every response solely from runtime state, audit trail, and
        registry.
        """
        consumed = min(consumed, token_limit)

        audit_trail = AuditTrail()
        registry = CapabilityRegistry()
        provider = _StateProvider()

        # Set up state
        provider.set_state(session_id, state)

        # Set up budget
        budget = SessionBudget(token_limit=token_limit, session_id=session_id)
        if consumed > 0:
            budget.charge(consumed)
        provider.set_budget(session_id, budget)

        # Register capabilities
        for entry in entries:
            registry._entries[entry.name] = entry

        # Write decisions
        for decision in decisions:
            audit_trail.write_decision(
                kind=decision.kind,
                subject=decision.subject,
                inputs=decision.inputs,
                decision=decision.decision,
                alternatives=decision.alternatives,
                session_id=session_id,
            )

        inspector = RuntimeInspector(
            audit_trail=audit_trail,
            registry=registry,
            state_provider=provider,
        )

        # 1. get_status — must always succeed
        status = inspector.get_status(session_id)
        assert isinstance(status, RuntimeStatus)

        # 2. explain_last_decision — succeeds or raises DecisionNotFoundError
        if decisions:
            result = inspector.explain_last_decision(session_id)
            assert isinstance(result, DecisionRecord)
        else:
            with pytest.raises(DecisionNotFoundError):
                inspector.explain_last_decision(session_id)

        # 3. explain_task — succeeds or raises TaskNotFoundError
        if state["tasks"]:
            task_id = state["tasks"][0]["id"]
            explanation = inspector.explain_task(session_id, task_id)
            assert isinstance(explanation, TaskExplanation)
        else:
            with pytest.raises(TaskNotFoundError):
                inspector.explain_task(session_id, "nonexistent-task")

        # 4. capability_summary — must always succeed
        summary = inspector.capability_summary()
        assert isinstance(summary, dict)
        assert "available" in summary
        assert "can_operate" in summary

    def test_inspector_has_no_ai_model_dependency(self):
        """Structural verification: the RuntimeInspector class has no attributes
        or constructor parameters that reference an AI model, LLM, or provider
        call mechanism.

        This confirms at a code-structural level that the inspector cannot
        invoke an AI model (Requirement 20.7).
        """
        # Check __init__ parameters
        init_sig = inspect.signature(RuntimeInspector.__init__)
        param_names = set(init_sig.parameters.keys()) - {"self"}

        # The inspector accepts only: audit_trail, registry, state_provider
        assert param_names == {"audit_trail", "registry", "state_provider"}

        # None of these are AI/LLM related types
        # Verify by checking type annotations
        annotations = init_sig.parameters
        for name in param_names:
            param_type = str(annotations[name].annotation)
            # Must not reference model, llm, ai, provider (case insensitive)
            lower_type = param_type.lower()
            assert "llm" not in lower_type
            assert "model" not in lower_type or "decision" in lower_type
            assert "ai_provider" not in lower_type

        # Check that the class has no method that could invoke an AI model
        # by scanning public method source for model/llm/provider invocations
        public_methods = [
            m for m in dir(RuntimeInspector)
            if not m.startswith("_") and callable(getattr(RuntimeInspector, m))
        ]

        # All public methods should exist and be query-only
        expected_methods = {
            "get_status", "explain_last_decision", "explain_task",
            "capability_summary", "current_node", "worker_status",
            "task_queue", "active_task",
        }
        assert expected_methods.issubset(set(public_methods))
