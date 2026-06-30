"""Unit tests for mode evaluation — minimum-capability validation and operational mode.

Tests Requirements 16.1–16.6 and 13.7:
- OPERATIONAL requires Coder role + VCS connector + coding tool
- DEGRADED mode with precise missing-capability reasons
- Soft degradations (Reviewer, vector store) recorded as DecisionRecord
- Mode transition events and records
- forge.ready emission with mode, summary, and startup report
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.runtime.events.models import DecisionKind, Event, EventType
from app.runtime.mode import ModeEvaluationResult, ModeEvaluator
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    OperationalMode,
    Role,
)
from app.runtime.registry import CapabilityRegistry


# --- Helpers ---


def _coder_entry() -> CapabilityEntry:
    """A healthy AI provider serving the Coder role."""
    return CapabilityEntry(
        name=Capability.AI_CODER,
        kind=CapabilityKind.AI_PROVIDER,
        healthy=True,
        roles=[Role.CODER],
        provider_name="openrouter",
    )


def _vcs_entry() -> CapabilityEntry:
    """A healthy VCS connector."""
    return CapabilityEntry(
        name=Capability.VCS_GITHUB,
        kind=CapabilityKind.VCS_CONNECTOR,
        healthy=True,
        roles=[],
        provider_name="github",
    )


def _coding_tool_entry() -> CapabilityEntry:
    """A healthy coding tool."""
    return CapabilityEntry(
        name=Capability.TOOL_AIDER,
        kind=CapabilityKind.CODING_TOOL,
        healthy=True,
        roles=[],
        provider_name="aider",
    )


def _reviewer_entry() -> CapabilityEntry:
    """A healthy AI provider serving the Reviewer role."""
    return CapabilityEntry(
        name=Capability.AI_REVIEWER,
        kind=CapabilityKind.AI_PROVIDER,
        healthy=True,
        roles=[Role.REVIEWER],
        provider_name="openrouter",
    )


def _vector_store_entry() -> CapabilityEntry:
    """A healthy vector store."""
    return CapabilityEntry(
        name=Capability.VECTOR_CHROMA,
        kind=CapabilityKind.VECTOR_STORE,
        healthy=True,
        roles=[],
        provider_name="chroma",
    )


async def _register_all(
    registry: CapabilityRegistry, entries: list[CapabilityEntry]
) -> None:
    """Register multiple capabilities."""
    for entry in entries:
        await registry.register(entry)


class EventCollector:
    """Collects emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# --- Tests ---


class TestOperationalModeRequirement:
    """Requirement 16.1: Coder role + VCS connector + coding tool for OPERATIONAL."""

    async def test_all_required_capabilities_present_gives_operational(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.OPERATIONAL
        assert result.summary.can_operate is True
        assert result.summary.mode == "operational"
        assert len(result.summary.missing_required) == 0
        assert result.summary.missing_reasons == {}

    async def test_missing_coder_gives_degraded(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.DEGRADED
        assert result.summary.can_operate is False
        assert Capability.AI_CODER in result.summary.missing_required
        assert "coder_role" in result.summary.missing_reasons

    async def test_missing_vcs_gives_degraded(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.DEGRADED
        assert result.summary.can_operate is False
        assert Capability.VCS_GITHUB in result.summary.missing_required
        assert "vcs_connector" in result.summary.missing_reasons

    async def test_missing_coding_tool_gives_degraded(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.DEGRADED
        assert result.summary.can_operate is False
        assert Capability.TOOL_AIDER in result.summary.missing_required
        assert "coding_tool" in result.summary.missing_reasons

    async def test_all_required_missing_gives_degraded_with_all_reasons(self) -> None:
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.DEGRADED
        assert result.summary.can_operate is False
        assert len(result.summary.missing_required) == 3
        assert len(result.summary.missing_reasons) == 3
        assert "coder_role" in result.summary.missing_reasons
        assert "vcs_connector" in result.summary.missing_reasons
        assert "coding_tool" in result.summary.missing_reasons


class TestDegradedModeReasons:
    """Requirement 16.1: Precise missing-capability reasons in CapabilitySummary."""

    async def test_missing_reasons_contain_descriptive_text(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_vcs_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert "coder_role" in result.summary.missing_reasons
        assert "Coder role" in result.summary.missing_reasons["coder_role"]
        assert "coding_tool" in result.summary.missing_reasons
        assert "coding tool" in result.summary.missing_reasons["coding_tool"]

    async def test_startup_report_includes_missing_reasons(self) -> None:
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        report = result.startup_report
        assert report["mode"] == "degraded"
        assert len(report["missing_required"]) == 3
        assert "missing_reasons" in report


class TestSoftDegradations:
    """Requirement 16.4: Soft degradations recorded as DecisionRecord."""

    async def test_missing_reviewer_recorded_as_decision(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])
        # No reviewer registered

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.OPERATIONAL
        # Should have soft degradation decisions
        reviewer_decisions = [
            d for d in result.decisions if d.subject == "reviewer_role"
        ]
        assert len(reviewer_decisions) == 1
        assert reviewer_decisions[0].kind == DecisionKind.CAPABILITY_TRANSITION
        assert reviewer_decisions[0].decision == "soft_degradation"
        assert "Reviewer" in reviewer_decisions[0].rationale

    async def test_missing_vector_store_recorded_as_decision(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])
        # No vector store registered

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.OPERATIONAL
        vector_decisions = [
            d for d in result.decisions if d.subject == "vector_store"
        ]
        assert len(vector_decisions) == 1
        assert vector_decisions[0].kind == DecisionKind.CAPABILITY_TRANSITION
        assert vector_decisions[0].decision == "soft_degradation"
        assert "Vector store" in vector_decisions[0].rationale

    async def test_all_soft_capabilities_present_no_decisions(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(
            registry,
            [
                _coder_entry(),
                _vcs_entry(),
                _coding_tool_entry(),
                _reviewer_entry(),
                _vector_store_entry(),
            ],
        )

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.OPERATIONAL
        assert len(result.decisions) == 0
        assert len(result.summary.soft_degradations) == 0

    async def test_soft_degradations_in_summary(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        # Both reviewer and vector store are soft degradations
        assert len(result.summary.soft_degradations) == 2

    async def test_soft_degradations_not_recorded_when_degraded(self) -> None:
        """When in DEGRADED mode, soft degradations are not recorded."""
        registry = CapabilityRegistry()
        # Missing required capability — no coder
        await _register_all(registry, [_vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.DEGRADED
        # No soft degradation decisions when not operational
        assert len(result.decisions) == 0
        assert len(result.summary.soft_degradations) == 0


class TestModeTransitionEvents:
    """Requirement 16.5: Emit capability transition event and DecisionRecord on mode changes."""

    async def test_mode_transition_emits_event(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")

        # First evaluation: starts as DEGRADED (no initial mode set)
        await evaluator.evaluate_and_emit()

        # Now add required capabilities — mode should transition to OPERATIONAL
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])
        result = await evaluator.evaluate_and_emit()

        assert result.mode == OperationalMode.OPERATIONAL

        # Should have emitted a CAPABILITY_RECOVERED event for the transition
        recovered_events = collector.events_of_type(EventType.CAPABILITY_RECOVERED)
        assert len(recovered_events) == 1
        assert recovered_events[0].payload["new_mode"] == "operational"
        assert recovered_events[0].payload["previous_mode"] == "degraded"

    async def test_transition_to_degraded_emits_degraded_event(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")

        # First evaluation: OPERATIONAL
        await evaluator.evaluate_and_emit()
        assert evaluator.current_mode == OperationalMode.OPERATIONAL

        # Remove a required capability
        await registry.deregister(Capability.AI_CODER)

        # Re-evaluate — should transition to DEGRADED
        result = await evaluator.evaluate_and_emit()
        assert result.mode == OperationalMode.DEGRADED

        degraded_events = collector.events_of_type(EventType.CAPABILITY_DEGRADED)
        assert len(degraded_events) == 1
        assert degraded_events[0].payload["new_mode"] == "degraded"
        assert degraded_events[0].payload["previous_mode"] == "operational"

    async def test_transition_records_decision_record(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")

        # First evaluation sets mode to OPERATIONAL
        await evaluator.evaluate_and_emit()

        # Remove required cap
        await registry.deregister(Capability.VCS_GITHUB)

        # Re-evaluate
        result = await evaluator.evaluate_and_emit()

        # Find the transition decision
        transition_decisions = [
            d for d in result.decisions if d.subject == "operational_mode"
        ]
        assert len(transition_decisions) == 1
        assert transition_decisions[0].kind == DecisionKind.CAPABILITY_TRANSITION
        assert transition_decisions[0].decision == "transition_to_degraded"
        assert "operational" in transition_decisions[0].rationale
        assert "degraded" in transition_decisions[0].rationale

    async def test_no_transition_event_when_mode_unchanged(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")

        # First evaluation: OPERATIONAL
        await evaluator.evaluate_and_emit()
        # Clear collected events from first evaluation
        collector.events.clear()

        # Second evaluation with same state: still OPERATIONAL
        result = await evaluator.evaluate_and_emit()

        # No transition events should be emitted
        recovered = collector.events_of_type(EventType.CAPABILITY_RECOVERED)
        degraded = collector.events_of_type(EventType.CAPABILITY_DEGRADED)
        assert len(recovered) == 0
        assert len(degraded) == 0

        # No transition decisions
        transition_decisions = [
            d for d in result.decisions if d.subject == "operational_mode"
        ]
        assert len(transition_decisions) == 0


class TestForgeReadyEmission:
    """Requirement 13.7: Emit forge.ready with operational mode, CapabilitySummary, and startup report."""

    async def test_forge_ready_emitted_on_evaluate_and_emit(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")
        result = await evaluator.evaluate_and_emit()

        ready_events = collector.events_of_type(EventType.FORGE_READY)
        assert len(ready_events) == 1

        payload = ready_events[0].payload
        assert payload["mode"] == "operational"
        assert payload["can_operate"] is True
        assert "startup_report" in payload

    async def test_forge_ready_in_degraded_mode(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")
        result = await evaluator.evaluate_and_emit()

        ready_events = collector.events_of_type(EventType.FORGE_READY)
        assert len(ready_events) == 1

        payload = ready_events[0].payload
        assert payload["mode"] == "degraded"
        assert payload["can_operate"] is False
        assert len(payload["missing_required"]) == 3
        assert len(payload["missing_reasons"]) == 3

    async def test_forge_ready_includes_startup_report(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")
        await evaluator.evaluate_and_emit()

        ready_events = collector.events_of_type(EventType.FORGE_READY)
        report = ready_events[0].payload["startup_report"]

        assert report["mode"] == "operational"
        assert report["available_capabilities"] == 3
        assert report["degraded_capabilities"] == 0
        assert report["missing_required"] == []

    async def test_forge_ready_includes_soft_degradations(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])
        # No reviewer and no vector store

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test-session")
        await evaluator.evaluate_and_emit()

        ready_events = collector.events_of_type(EventType.FORGE_READY)
        payload = ready_events[0].payload

        assert len(payload["soft_degradations"]) == 2

    async def test_forge_ready_event_source_is_mode_evaluator(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="sys")
        await evaluator.evaluate_and_emit()

        ready_events = collector.events_of_type(EventType.FORGE_READY)
        assert ready_events[0].source == "mode_evaluator"
        assert ready_events[0].session_id == "sys"


class TestModeEvaluatorCurrentMode:
    """Test that current_mode property tracks state correctly."""

    async def test_current_mode_none_before_evaluation(self) -> None:
        registry = CapabilityRegistry()
        evaluator = ModeEvaluator(registry)
        assert evaluator.current_mode is None

    async def test_current_mode_set_after_evaluate_and_emit(self) -> None:
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector)
        await evaluator.evaluate_and_emit()

        assert evaluator.current_mode == OperationalMode.OPERATIONAL

    async def test_first_evaluation_does_not_emit_transition(self) -> None:
        """First evaluation sets mode but doesn't emit transition (no prior mode)."""
        collector = EventCollector()
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry, event_emitter=collector)
        await evaluator.evaluate_and_emit()

        # No transition events on first evaluation
        recovered = collector.events_of_type(EventType.CAPABILITY_RECOVERED)
        degraded = collector.events_of_type(EventType.CAPABILITY_DEGRADED)
        assert len(recovered) == 0
        assert len(degraded) == 0


class TestModeWithNoEventEmitter:
    """Test that the evaluator works without an event emitter configured."""

    async def test_evaluate_works_without_emitter(self) -> None:
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry)
        result = evaluator.evaluate()

        assert result.mode == OperationalMode.OPERATIONAL

    async def test_evaluate_and_emit_works_without_emitter(self) -> None:
        registry = CapabilityRegistry()

        evaluator = ModeEvaluator(registry)
        result = await evaluator.evaluate_and_emit()

        assert result.mode == OperationalMode.DEGRADED
        assert evaluator.current_mode == OperationalMode.DEGRADED


class TestRequiredCapabilityLossTriggersDegraded:
    """Requirement 16.6: Required capability loss triggers transition to DEGRADED."""

    async def test_deregistering_coder_causes_degraded_transition(self) -> None:
        """Deregistering the Coder role provider transitions from OPERATIONAL to DEGRADED."""
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test")

        # Initial evaluation: OPERATIONAL
        result = await evaluator.evaluate_and_emit()
        assert result.mode == OperationalMode.OPERATIONAL
        collector.events.clear()

        # Deregister the coder (simulates HealthMonitor deregistration on failure threshold)
        await registry.deregister(Capability.AI_CODER)

        # Re-evaluate: should now be DEGRADED
        result = await evaluator.evaluate_and_emit()
        assert result.mode == OperationalMode.DEGRADED
        assert evaluator.current_mode == OperationalMode.DEGRADED

        # Should have emitted CAPABILITY_DEGRADED for mode transition
        degraded_events = collector.events_of_type(EventType.CAPABILITY_DEGRADED)
        assert len(degraded_events) == 1
        assert degraded_events[0].payload["new_mode"] == "degraded"
        assert degraded_events[0].payload["previous_mode"] == "operational"

        # Summary should identify missing coder
        assert "coder_role" in result.summary.missing_reasons

    async def test_deregistering_vcs_causes_degraded_transition(self) -> None:
        """Deregistering VCS connector transitions from OPERATIONAL to DEGRADED."""
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test")
        await evaluator.evaluate_and_emit()
        collector.events.clear()

        await registry.deregister(Capability.VCS_GITHUB)

        result = await evaluator.evaluate_and_emit()
        assert result.mode == OperationalMode.DEGRADED
        assert "vcs_connector" in result.summary.missing_reasons

    async def test_deregistering_coding_tool_causes_degraded_transition(self) -> None:
        """Deregistering coding tool transitions from OPERATIONAL to DEGRADED."""
        collector = EventCollector()
        registry = CapabilityRegistry()
        await _register_all(registry, [_coder_entry(), _vcs_entry(), _coding_tool_entry()])

        evaluator = ModeEvaluator(registry, event_emitter=collector, session_id="test")
        await evaluator.evaluate_and_emit()
        collector.events.clear()

        await registry.deregister(Capability.TOOL_AIDER)

        result = await evaluator.evaluate_and_emit()
        assert result.mode == OperationalMode.DEGRADED
        assert "coding_tool" in result.summary.missing_reasons
