"""Unit tests for the PolicyEngine with retry/escalate/skip decisions.

Tests deterministic decision-making, event emission, DecisionRecord writing,
escalation with registry checks, retry budget exhaustion, and task.fail events.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7
"""

from __future__ import annotations

import pytest

from app.runtime.events.models import DecisionKind, Event, EventType
from app.runtime.policies import (
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    PolicyResult,
    PolicyRule,
    parse_policy_config,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeEventCollector:
    """Collects emitted events for assertions."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def events_of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_default_config() -> PolicyConfig:
    """Create a default PolicyConfig matching the standard policies.yaml."""
    return PolicyConfig(
        max_retries_per_task=3,
        escalation_failure_threshold=2,
        rules=[
            PolicyRule(
                on="verification_failure", decision="retry", stage="tests", max_attempts=3
            ),
            PolicyRule(
                on="verification_failure", decision="retry", stage="lint", max_attempts=2
            ),
            PolicyRule(
                on="verification_failure", decision="retry", stage="type_check", max_attempts=2
            ),
            PolicyRule(
                on="verification_failure", decision="skip", stage="security", max_attempts=1
            ),
            PolicyRule(on="tool_failure", decision="escalate", threshold=2),
            PolicyRule(on="budget_exceeded", decision="skip"),
        ],
    )


def make_registry_checker(available: bool = False):
    """Create a registry checker that returns fixed availability."""

    def checker(tool_name: str) -> bool:
        return available

    return checker


# ---------------------------------------------------------------------------
# Tests: Deterministic decision from rules + attempt count (Req 8.1)
# ---------------------------------------------------------------------------


class TestDeterministicDecision:
    """PolicyEngine deterministically decides retry, escalate, or skip."""

    async def test_retry_on_tests_failure_first_attempt(self) -> None:
        """First attempt on 'tests' stage failure -> retry."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.RETRY
        assert result.task_id == "task-1"
        assert result.stage == "tests"

    async def test_retry_on_lint_failure(self) -> None:
        """Lint failure -> retry (per rules)."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="lint",
            attempt_count=1,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.RETRY

    async def test_security_stage_max_attempts_reached(self) -> None:
        """Security failure with attempt >= max_attempts -> failed."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        # Security rule has max_attempts=1, so attempt_count=1 triggers failed
        result = await engine.decide(
            task_id="task-1",
            stage_name="security",
            attempt_count=1,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True

    async def test_skip_on_unknown_stage(self) -> None:
        """Unknown stage with no matching rule -> default skip."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="unknown_stage",
            attempt_count=1,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.SKIP
        assert result.rule_matched == "default"

    async def test_deterministic_same_inputs_same_output(self) -> None:
        """Same inputs always produce the same decision."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        results = []
        for _ in range(5):
            result = await engine.decide(
                task_id="task-1",
                stage_name="tests",
                attempt_count=1,
                tool_failures=0,
            )
            results.append(result.decision)

        # All identical
        assert all(r == PolicyDecision.RETRY for r in results)

    async def test_stage_specific_max_attempts_exhausted(self) -> None:
        """Stage max_attempts reached -> task failed."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        # lint has max_attempts=2, attempt_count=2 -> failed
        result = await engine.decide(
            task_id="task-1",
            stage_name="lint",
            attempt_count=2,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True


# ---------------------------------------------------------------------------
# Tests: Emit exactly one policy.decision event per decision (Req 8.2)
# ---------------------------------------------------------------------------


class TestPolicyDecisionEvent:
    """Emit exactly one policy.decision event per decide() call."""

    async def test_one_event_per_decision(self) -> None:
        """Each decide() call emits exactly one policy.decision event."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        policy_events = collector.events_of_type(EventType.POLICY_DECISION)
        assert len(policy_events) == 1

    async def test_event_carries_correct_payload(self) -> None:
        """policy.decision event carries subject, decision, rule, and inputs."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-42",
            stage_name="tests",
            attempt_count=2,
            tool_failures=0,
        )

        policy_events = collector.events_of_type(EventType.POLICY_DECISION)
        payload = policy_events[0].payload
        assert payload["task_id"] == "task-42"
        assert payload["stage"] == "tests"
        assert payload["decision"] == "retry"
        assert payload["attempt_count"] == 2
        assert "rule_matched" in payload

    async def test_event_has_correct_source_and_session(self) -> None:
        """policy.decision event has source='policy_engine' and correct session."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-99"
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        event = collector.events_of_type(EventType.POLICY_DECISION)[0]
        assert event.source == "policy_engine"
        assert event.session_id == "sess-99"

    async def test_multiple_decisions_emit_multiple_events(self) -> None:
        """Multiple decide() calls emit one event each."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(task_id="t1", stage_name="tests", attempt_count=1, tool_failures=0)
        await engine.decide(task_id="t2", stage_name="lint", attempt_count=1, tool_failures=0)
        await engine.decide(
            task_id="t3", stage_name="type_check", attempt_count=1, tool_failures=0
        )

        policy_events = collector.events_of_type(EventType.POLICY_DECISION)
        assert len(policy_events) == 3


# ---------------------------------------------------------------------------
# Tests: Write DecisionRecord for each decision (Req 8.3)
# ---------------------------------------------------------------------------


class TestDecisionRecord:
    """Write DecisionRecord for each decision."""

    async def test_decision_record_created(self) -> None:
        """Each decide() writes a DecisionRecord."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        assert len(engine.decisions) == 1

    async def test_decision_record_has_correct_fields(self) -> None:
        """DecisionRecord carries kind, subject, inputs, decision, rationale."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-5",
            stage_name="tests",
            attempt_count=2,
            tool_failures=0,
        )

        record = engine.decisions[0]
        assert record.kind == DecisionKind.RETRY
        assert record.subject == "task-5"
        assert record.inputs["task_id"] == "task-5"
        assert record.inputs["stage"] == "tests"
        assert record.inputs["attempt_count"] == 2
        assert record.decision == "retry"
        assert record.rationale != ""
        assert record.session_id == "sess-1"

    async def test_decision_record_alternatives(self) -> None:
        """DecisionRecord includes alternative decisions not taken."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        record = engine.decisions[0]
        # Alternatives should be all decisions except the one taken
        assert "retry" not in record.alternatives
        assert "escalate" in record.alternatives

    async def test_failed_decision_has_task_failed_kind(self) -> None:
        """A failed decision produces a TASK_FAILED DecisionRecord."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,  # >= max_retries_per_task
            tool_failures=0,
        )

        record = engine.decisions[0]
        assert record.kind == DecisionKind.TASK_FAILED

    async def test_multiple_decisions_accumulate(self) -> None:
        """Multiple decide() calls accumulate DecisionRecords."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(task_id="t1", stage_name="tests", attempt_count=1, tool_failures=0)
        await engine.decide(task_id="t2", stage_name="lint", attempt_count=1, tool_failures=0)

        assert len(engine.decisions) == 2
        assert engine.decisions[0].subject == "t1"
        assert engine.decisions[1].subject == "t2"


# ---------------------------------------------------------------------------
# Tests: Escalate to alternate coding tool on tool failure threshold (Req 8.4)
# ---------------------------------------------------------------------------


class TestEscalation:
    """Escalate to alternate coding tool on tool failure threshold."""

    async def test_escalate_when_tool_failures_reach_threshold(self) -> None:
        """Tool failures >= threshold with available alternate -> escalate."""
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            registry_checker=make_registry_checker(available=True),
            session_id="sess-1",
        )

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=2,  # == threshold
        )

        assert result.decision == PolicyDecision.ESCALATE
        assert result.escalation_target == "tool_openhands"
        assert result.task_failed is False

    async def test_escalate_event_carries_escalation_target(self) -> None:
        """Escalation event payload includes the escalation target."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            event_emitter=collector.emit,
            registry_checker=make_registry_checker(available=True),
            session_id="sess-1",
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=3,
        )

        event = collector.events_of_type(EventType.POLICY_DECISION)[0]
        assert event.payload["escalation_target"] == "tool_openhands"
        assert event.payload["decision"] == "escalate"

    async def test_escalation_decision_record_kind(self) -> None:
        """Escalation produces a DecisionRecord with ESCALATE kind."""
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            registry_checker=make_registry_checker(available=True),
            session_id="sess-1",
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=2,
        )

        record = engine.decisions[0]
        assert record.kind == DecisionKind.ESCALATE
        assert record.decision == "escalate"


# ---------------------------------------------------------------------------
# Tests: Mark task failed if escalation target unavailable (Req 8.5)
# ---------------------------------------------------------------------------


class TestEscalationTargetUnavailable:
    """Mark task failed if escalation target unavailable in Registry."""

    async def test_escalation_target_unavailable_marks_failed(self) -> None:
        """No alternate tool in registry -> task marked failed."""
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            registry_checker=make_registry_checker(available=False),
            session_id="sess-1",
        )

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=2,
        )

        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True
        assert result.escalation_target == "tool_openhands"
        assert "unavailable" in result.reason

    async def test_escalation_unavailable_emits_task_fail(self) -> None:
        """Unavailable escalation target emits task.fail event."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            event_emitter=collector.emit,
            registry_checker=make_registry_checker(available=False),
            session_id="sess-1",
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=2,
        )

        fail_events = collector.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 1
        assert fail_events[0].payload["task_id"] == "task-1"

    async def test_no_registry_checker_means_unavailable(self) -> None:
        """No registry_checker configured -> treat as unavailable."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=2,
        )

        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True


# ---------------------------------------------------------------------------
# Tests: Retry budget exhaustion (Req 8.6)
# ---------------------------------------------------------------------------


class TestRetryBudgetExhaustion:
    """On retry budget exhaustion: mark task failed."""

    async def test_retry_budget_exhausted_marks_task_failed(self) -> None:
        """attempt_count >= max_retries_per_task -> task failed."""
        config = make_default_config()  # max_retries_per_task = 3
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True
        assert "Retry budget exhausted" in result.reason

    async def test_retry_budget_not_exhausted_allows_retry(self) -> None:
        """attempt_count < max_retries_per_task -> retry allowed."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=2,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.RETRY
        assert result.task_failed is False

    async def test_retry_budget_exhaustion_takes_priority(self) -> None:
        """Budget exhaustion takes priority over escalation threshold."""
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            registry_checker=make_registry_checker(available=True),
            session_id="sess-1",
        )

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=5,  # Would normally trigger escalation
        )

        # Budget exhaustion has higher priority
        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True

    async def test_budget_exhaustion_emits_task_fail_event(self) -> None:
        """Budget exhaustion emits task.fail event."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        fail_events = collector.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 1

    async def test_budget_over_max_also_fails(self) -> None:
        """attempt_count > max_retries_per_task also fails."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=10,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.FAILED
        assert result.task_failed is True

    async def test_failed_task_tracked(self) -> None:
        """Failed task is tracked in engine state."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        assert engine.is_task_failed("task-1")
        assert not engine.is_task_failed("task-2")

    async def test_independent_tasks_can_execute(self) -> None:
        """Independent tasks can still execute after one task fails."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        # Independent task can still execute
        assert engine.can_execute("task-2")

    async def test_dependent_tasks_blocked(self) -> None:
        """Tasks depending on a failed task are blocked."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        # Dependent task is blocked
        assert not engine.can_execute("task-2", depends_on=["task-1"])
        # Non-dependent task is fine
        assert engine.can_execute("task-3", depends_on=["task-4"])


# ---------------------------------------------------------------------------
# Tests: Emit task.fail event (Req 8.7)
# ---------------------------------------------------------------------------


class TestTaskFailEvent:
    """Emit task.fail event with task ID, failing stage, reason, and policy decision."""

    async def test_task_fail_event_emitted_on_failure(self) -> None:
        """task.fail is emitted when task is marked failed."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-7",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        fail_events = collector.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 1

    async def test_task_fail_event_payload(self) -> None:
        """task.fail event carries task_id, stage, reason, policy_decision."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-7",
            stage_name="lint",
            attempt_count=3,
            tool_failures=0,
        )

        event = collector.events_of_type(EventType.TASK_FAIL)[0]
        assert event.payload["task_id"] == "task-7"
        assert event.payload["stage"] == "lint"
        assert "reason" in event.payload
        assert event.payload["policy_decision"] == "failed"

    async def test_task_fail_event_source_and_session(self) -> None:
        """task.fail event has source='policy_engine' and correct session."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-42"
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=3,
            tool_failures=0,
        )

        event = collector.events_of_type(EventType.TASK_FAIL)[0]
        assert event.source == "policy_engine"
        assert event.session_id == "sess-42"

    async def test_no_task_fail_event_on_retry(self) -> None:
        """No task.fail event when decision is retry."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        fail_events = collector.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 0

    async def test_no_task_fail_event_on_escalate(self) -> None:
        """No task.fail event when decision is escalate (successful)."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config,
            event_emitter=collector.emit,
            registry_checker=make_registry_checker(available=True),
            session_id="sess-1",
        )

        await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=2,
        )

        fail_events = collector.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 0

    async def test_no_task_fail_event_on_skip(self) -> None:
        """No task.fail event when decision is skip."""
        collector = FakeEventCollector()
        config = make_default_config()
        engine = PolicyEngine(
            config=config, event_emitter=collector.emit, session_id="sess-1"
        )

        await engine.decide(
            task_id="task-1",
            stage_name="unknown_stage",
            attempt_count=1,
            tool_failures=0,
        )

        fail_events = collector.events_of_type(EventType.TASK_FAIL)
        assert len(fail_events) == 0


# ---------------------------------------------------------------------------
# Tests: No event emitter configured
# ---------------------------------------------------------------------------


class TestNoEventEmitter:
    """PolicyEngine works correctly without an event emitter."""

    async def test_engine_works_without_emitter(self) -> None:
        """PolicyEngine runs fine when no event_emitter is configured."""
        config = make_default_config()
        engine = PolicyEngine(config=config, session_id="sess-1")

        result = await engine.decide(
            task_id="task-1",
            stage_name="tests",
            attempt_count=1,
            tool_failures=0,
        )

        assert result.decision == PolicyDecision.RETRY
        assert len(engine.decisions) == 1


# ---------------------------------------------------------------------------
# Tests: parse_policy_config
# ---------------------------------------------------------------------------


class TestParsePolicyConfig:
    """Test parsing of policies.yaml into PolicyConfig."""

    def test_parse_standard_config(self) -> None:
        """Parse a standard policies.yaml structure."""
        raw = {
            "retry_budget": {"max_retries_per_task": 5},
            "escalation": {"failure_threshold": 3},
            "rules": [
                {
                    "on": "verification_failure",
                    "stage": "tests",
                    "decision": "retry",
                    "max_attempts": 4,
                },
                {"on": "tool_failure", "decision": "escalate", "threshold": 3},
            ],
        }

        config = parse_policy_config(raw)

        assert config.max_retries_per_task == 5
        assert config.escalation_failure_threshold == 3
        assert len(config.rules) == 2
        assert config.rules[0].on == "verification_failure"
        assert config.rules[0].stage == "tests"
        assert config.rules[0].max_attempts == 4
        assert config.rules[1].on == "tool_failure"
        assert config.rules[1].threshold == 3

    def test_parse_empty_config_uses_defaults(self) -> None:
        """Empty config produces defaults."""
        config = parse_policy_config({})

        assert config.max_retries_per_task == 3
        assert config.escalation_failure_threshold == 2
        assert config.rules == []

    def test_parse_partial_config(self) -> None:
        """Partial config uses defaults for missing fields."""
        raw = {"retry_budget": {"max_retries_per_task": 10}}

        config = parse_policy_config(raw)

        assert config.max_retries_per_task == 10
        assert config.escalation_failure_threshold == 2
        assert config.rules == []
