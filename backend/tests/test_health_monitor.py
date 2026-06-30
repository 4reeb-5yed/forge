"""Unit tests for the HealthMonitor.

Tests cover:
- Configurable interval clamping
- Consecutive failure counting and deregistration
- Recovery threshold and re-registration
- Event emission on transitions (capability.degraded / capability.recovered)
- DecisionRecord writing on transitions
- Probe timeout handling
- Background loop start/stop

Requirements: 14.1, 14.2, 14.3, 14.4, 14.5
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from app.runtime.events.models import DecisionRecord, Event, EventType
from app.runtime.health import HealthMonitor, HealthMonitorConfig, _CapabilityHealthState
from app.runtime.models import (
    Capability,
    CapabilityEntry,
    CapabilityKind,
    Role,
)
from app.runtime.registry import CapabilityRegistry
from app.runtime.types import Health


# --- Test Fixtures / Helpers ---


class FakeProbe:
    """A configurable fake probe for testing."""

    def __init__(self, healthy: bool = True, *, name: str = "fake"):
        self.name = name
        self.healthy = healthy
        self._kind = CapabilityKind.AI_PROVIDER
        self._roles: list[Role] = [Role.CODER]
        self.call_count = 0
        self._delay: float = 0.0
        self._raise_on_call: Exception | None = None

    async def health_check(self) -> Health:
        self.call_count += 1
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._raise_on_call is not None:
            raise self._raise_on_call
        if self.healthy:
            return Health.healthy(latency_ms=5.0)
        return Health.unhealthy("probe says unhealthy")

    def set_healthy(self, healthy: bool) -> None:
        self.healthy = healthy

    def set_delay(self, delay: float) -> None:
        self._delay = delay

    def set_raise(self, exc: Exception | None) -> None:
        self._raise_on_call = exc


class EventCollector:
    """Collects emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> Event:
        self.events.append(event)
        return event

    def of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


class DecisionCollector:
    """Collects written DecisionRecords for assertion."""

    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []

    async def __call__(self, record: DecisionRecord) -> None:
        self.records.append(record)


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry(session_id="test")


@pytest.fixture
def events() -> EventCollector:
    return EventCollector()


@pytest.fixture
def decisions() -> DecisionCollector:
    return DecisionCollector()


# --- Configuration Clamping Tests ---


class TestHealthMonitorConfig:
    """Test that config values are clamped to their valid bounds."""

    def test_default_values(self) -> None:
        config = HealthMonitorConfig()
        assert config.probe_interval_s == 30.0
        assert config.probe_timeout_s == 10.0
        assert config.failure_threshold == 3
        assert config.recovery_threshold == 2

    def test_clamps_probe_interval_low(self) -> None:
        config = HealthMonitorConfig(probe_interval_s=1.0)
        assert config.probe_interval_s == 5.0

    def test_clamps_probe_interval_high(self) -> None:
        config = HealthMonitorConfig(probe_interval_s=500.0)
        assert config.probe_interval_s == 300.0

    def test_clamps_probe_timeout_low(self) -> None:
        config = HealthMonitorConfig(probe_timeout_s=0.5)
        assert config.probe_timeout_s == 1.0

    def test_clamps_probe_timeout_high(self) -> None:
        config = HealthMonitorConfig(probe_timeout_s=120.0)
        assert config.probe_timeout_s == 60.0

    def test_clamps_failure_threshold_low(self) -> None:
        config = HealthMonitorConfig(failure_threshold=0)
        assert config.failure_threshold == 1

    def test_clamps_failure_threshold_high(self) -> None:
        config = HealthMonitorConfig(failure_threshold=20)
        assert config.failure_threshold == 10

    def test_clamps_recovery_threshold_low(self) -> None:
        config = HealthMonitorConfig(recovery_threshold=0)
        assert config.recovery_threshold == 1

    def test_clamps_recovery_threshold_high(self) -> None:
        config = HealthMonitorConfig(recovery_threshold=20)
        assert config.recovery_threshold == 10


# --- Consecutive Failure and Deregistration Tests ---


class TestFailureTracking:
    """Test that failures are tracked and deregistration happens at threshold."""

    async def test_single_failure_keeps_capability_registered(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """Req 14.2: single failure below threshold keeps capability registered."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=3
            ),
        )

        # Probe once — should not deregister
        await monitor._probe_one(Capability.AI_CODER)

        assert registry.has(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state is not None
        assert state.consecutive_failures == 1
        assert not state.degraded
        assert len(events.of_type(EventType.CAPABILITY_DEGRADED)) == 0

    async def test_deregisters_at_failure_threshold(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """Req 14.3: deregister after consecutive failure threshold."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=3
            ),
        )

        # Probe 3 times to hit threshold
        for _ in range(3):
            await monitor._probe_one(Capability.AI_CODER)

        # Should be deregistered now
        assert not registry.has(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state is not None
        assert state.degraded is True
        assert state.consecutive_failures == 3

        # Should have emitted capability.degraded
        degraded_events = events.of_type(EventType.CAPABILITY_DEGRADED)
        assert len(degraded_events) == 1
        assert degraded_events[0].payload["capability"] == "ai_coder"

        # Should have written a DecisionRecord
        assert len(decisions.records) == 1
        assert decisions.records[0].decision == "deregister"
        assert decisions.records[0].subject == "ai_coder"

    async def test_failure_count_resets_on_success(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """A successful probe resets the consecutive failure counter."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=3
            ),
        )

        # Two failures
        await monitor._probe_one(Capability.AI_CODER)
        await monitor._probe_one(Capability.AI_CODER)
        assert monitor.get_state(Capability.AI_CODER).consecutive_failures == 2

        # One success — resets counter
        probe.set_healthy(True)
        await monitor._probe_one(Capability.AI_CODER)
        assert monitor.get_state(Capability.AI_CODER).consecutive_failures == 0
        assert registry.has(Capability.AI_CODER)

    async def test_timeout_counts_as_failure(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """Req 14.1: per-probe timeout exceeded counts as failure."""
        probe = FakeProbe(healthy=True)
        probe.set_delay(5.0)  # Will exceed 1s timeout

        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=2
            ),
        )

        # Probe twice — both will timeout and count as failures
        await monitor._probe_one(Capability.AI_CODER)
        await monitor._probe_one(Capability.AI_CODER)

        assert not registry.has(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state.degraded is True
        assert "timed out" in state.last_reason.lower()

    async def test_exception_counts_as_failure(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """A probe that raises an exception counts as a failure."""
        probe = FakeProbe(healthy=True)
        probe.set_raise(RuntimeError("connection refused"))

        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=1
            ),
        )

        await monitor._probe_one(Capability.AI_CODER)

        assert not registry.has(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state.degraded is True
        assert "connection refused" in state.last_reason


# --- Recovery Tests ---


class TestRecovery:
    """Test that degraded capabilities recover after consecutive healthy probes."""

    async def test_recovery_after_threshold_healthy_probes(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """Req 14.4: re-register after recovery_threshold consecutive healthy probes."""
        probe = FakeProbe(healthy=False, name="ai_coder_provider")
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5,
                probe_timeout_s=1,
                failure_threshold=2,
                recovery_threshold=2,
            ),
        )

        # Degrade the capability (2 failures)
        await monitor._probe_one(Capability.AI_CODER)
        await monitor._probe_one(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)

        # Now the probe becomes healthy
        probe.set_healthy(True)

        # First recovery probe
        await monitor._probe_one(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)  # Not yet recovered

        # Second recovery probe — should recover
        await monitor._probe_one(Capability.AI_CODER)
        assert registry.has(Capability.AI_CODER)

        # Should have emitted capability.recovered
        recovered_events = events.of_type(EventType.CAPABILITY_RECOVERED)
        assert len(recovered_events) == 1
        assert recovered_events[0].payload["capability"] == "ai_coder"

        # Should have written a recovery DecisionRecord
        recovery_decisions = [
            r for r in decisions.records if r.decision == "re-register"
        ]
        assert len(recovery_decisions) == 1

    async def test_recovery_resets_on_failure(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """A failure during recovery resets the recovery counter."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5,
                probe_timeout_s=1,
                failure_threshold=1,
                recovery_threshold=3,
            ),
        )

        # Degrade
        await monitor._probe_one(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)

        # Start recovery
        probe.set_healthy(True)
        await monitor._probe_one(Capability.AI_CODER)
        await monitor._probe_one(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state.consecutive_recoveries == 2

        # Failure during recovery — resets counter
        probe.set_healthy(False)
        await monitor._probe_one(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state.consecutive_recoveries == 0
        assert not registry.has(Capability.AI_CODER)  # Still degraded


# --- Event Emission Tests ---


class TestEventEmission:
    """Test that correct events are emitted on transitions."""

    async def test_no_events_when_no_emitter(
        self, registry: CapabilityRegistry, decisions: DecisionCollector
    ) -> None:
        """No crash when event_emitter is None."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=None,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=1
            ),
        )

        # Should not crash
        await monitor._probe_one(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)

    async def test_degraded_event_payload(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """Degraded events carry capability name and failure reason."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=1
            ),
        )

        await monitor._probe_one(Capability.AI_CODER)

        degraded = events.of_type(EventType.CAPABILITY_DEGRADED)
        assert len(degraded) == 1
        payload = degraded[0].payload
        assert payload["capability"] == "ai_coder"
        assert "reason" in payload
        assert payload["consecutive_failures"] == 1
        assert payload["failure_threshold"] == 1


# --- Background Loop Tests ---


class TestMonitorLoop:
    """Test the background monitoring loop start/stop."""

    async def test_start_and_stop(self, registry: CapabilityRegistry) -> None:
        """Monitor can be started and stopped cleanly."""
        monitor = HealthMonitor(
            registry=registry,
            probe_map={},
            config=HealthMonitorConfig(probe_interval_s=5, probe_timeout_s=1),
        )

        await monitor.start()
        assert monitor.running is True

        await monitor.stop()
        assert monitor.running is False

    async def test_start_is_idempotent(self, registry: CapabilityRegistry) -> None:
        """Calling start() twice does not create multiple tasks."""
        monitor = HealthMonitor(
            registry=registry,
            probe_map={},
            config=HealthMonitorConfig(probe_interval_s=5, probe_timeout_s=1),
        )

        await monitor.start()
        task1 = monitor._task

        await monitor.start()
        task2 = monitor._task

        assert task1 is task2
        await monitor.stop()

    async def test_stop_when_not_running(self, registry: CapabilityRegistry) -> None:
        """Calling stop() when not running has no effect."""
        monitor = HealthMonitor(
            registry=registry,
            probe_map={},
            config=HealthMonitorConfig(probe_interval_s=5, probe_timeout_s=1),
        )

        # Should not raise
        await monitor.stop()
        assert monitor.running is False

    async def test_probes_registered_capabilities(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """The monitor loop probes all registered capabilities."""
        probe = FakeProbe(healthy=True)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(probe_interval_s=5, probe_timeout_s=1),
        )

        # Directly call _probe_all to test without sleeping
        await monitor._probe_all()

        assert probe.call_count == 1
        assert registry.has(Capability.AI_CODER)


# --- No Probe Function Tests ---


class TestProbeAll:
    """Test _probe_all probes both registered and degraded capabilities."""

    async def test_probe_all_probes_multiple_registered_capabilities(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """_probe_all probes all currently registered capabilities."""
        probe_coder = FakeProbe(healthy=True, name="coder")
        probe_tool = FakeProbe(healthy=True, name="tool")
        probe_tool._kind = CapabilityKind.CODING_TOOL
        probe_tool._roles = []

        entry_coder = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        entry_tool = CapabilityEntry(
            name=Capability.TOOL_AIDER,
            kind=CapabilityKind.CODING_TOOL,
            healthy=True,
            roles=[],
        )
        await registry.register(entry_coder)
        await registry.register(entry_tool)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={
                Capability.AI_CODER: probe_coder,
                Capability.TOOL_AIDER: probe_tool,
            },
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(probe_interval_s=5, probe_timeout_s=1),
        )

        await monitor._probe_all()

        assert probe_coder.call_count == 1
        assert probe_tool.call_count == 1

    async def test_probe_all_includes_degraded_capabilities_for_recovery(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """_probe_all probes degraded capabilities even though they are not registered (Req 14.4)."""
        probe = FakeProbe(healthy=False, name="coder")
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
            roles=[Role.CODER],
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=1, recovery_threshold=1
            ),
        )

        # Degrade the capability
        await monitor._probe_one(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)
        assert monitor.get_state(Capability.AI_CODER).degraded is True
        probe.call_count = 0  # Reset count

        # _probe_all should still probe degraded capability (for recovery)
        probe.set_healthy(True)
        await monitor._probe_all()

        assert probe.call_count == 1
        # Should have recovered since recovery_threshold is 1
        assert registry.has(Capability.AI_CODER)


class TestNoProbeFn:
    """Test behavior when no probe function is available for a capability."""

    async def test_no_probe_fn_counts_as_failure(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """Missing probe function is treated as a failure."""
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={},  # No probe for AI_CODER
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=1
            ),
        )

        await monitor._probe_one(Capability.AI_CODER)

        assert not registry.has(Capability.AI_CODER)
        state = monitor.get_state(Capability.AI_CODER)
        assert state.degraded is True


# --- Decision Record Tests ---


class TestDecisionRecords:
    """Test that DecisionRecords are written on capability transitions."""

    async def test_no_crash_when_no_audit_writer(
        self, registry: CapabilityRegistry, events: EventCollector
    ) -> None:
        """No crash when audit_writer is None."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=None,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=1
            ),
        )

        # Should not crash
        await monitor._probe_one(Capability.AI_CODER)
        assert not registry.has(Capability.AI_CODER)

    async def test_degrade_decision_record_content(
        self, registry: CapabilityRegistry, events: EventCollector, decisions: DecisionCollector
    ) -> None:
        """DecisionRecord on degradation has correct kind and content."""
        probe = FakeProbe(healthy=False)
        entry = CapabilityEntry(
            name=Capability.AI_CODER,
            kind=CapabilityKind.AI_PROVIDER,
            healthy=True,
        )
        await registry.register(entry)

        monitor = HealthMonitor(
            registry=registry,
            probe_map={Capability.AI_CODER: probe},
            event_emitter=events,
            audit_writer=decisions,
            config=HealthMonitorConfig(
                probe_interval_s=5, probe_timeout_s=1, failure_threshold=2
            ),
        )

        await monitor._probe_one(Capability.AI_CODER)
        await monitor._probe_one(Capability.AI_CODER)

        assert len(decisions.records) == 1
        record = decisions.records[0]
        from app.runtime.events.models import DecisionKind
        assert record.kind == DecisionKind.CAPABILITY_TRANSITION
        assert record.subject == "ai_coder"
        assert record.decision == "deregister"
        assert "consecutive_failures" in record.inputs
        assert "2" in record.rationale or "failed 2" in record.rationale
        assert "keep_registered" in record.alternatives
