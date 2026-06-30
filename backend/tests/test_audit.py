"""Unit tests for the Audit Trail module.

Tests cover:
- Event persistence to audit_log with unique key (session_id, seq)
- DecisionRecord writes with deterministic rationale
- Duplicate (session_id, seq) rejection (idempotent)
- Write failure handling: retain event, surface error
- Query methods: get_events, get_decisions, get_decision_by_subject
- EventBus subscription integration

Requirements: 19.1, 19.2, 19.3, 19.5, 19.6
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.audit import AuditTrail, AuditWriteError
from app.runtime.events.bus import EventBus
from app.runtime.events.models import DecisionKind, Event, EventType


def _make_event(
    session_id: str = "session-1",
    seq: int = 1,
    event_type: EventType = EventType.TASK_START,
    source: str = "test",
    payload: dict | None = None,
    correlation_id: str | None = None,
    event_id: str | None = None,
) -> Event:
    """Helper to create a test Event."""
    return Event(
        schema_version=1,
        seq=seq,
        session_id=session_id,
        type=event_type,
        timestamp=datetime.now(timezone.utc),
        source=source,
        payload=payload or {"task_id": "t1"},
        correlation_id=correlation_id,
        event_id=event_id or str(uuid.uuid4()),
    )


class TestAuditTrailEventPersistence:
    """Tests for event persistence to audit_log (Req 19.3)."""

    async def test_persist_single_event(self) -> None:
        """A single event is persisted and retrievable."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=1)

        await trail.handle_event(event)

        events = trail.get_events("s1")
        assert len(events) == 1
        assert events[0] is event

    async def test_persist_multiple_events_ordered_by_seq(self) -> None:
        """Multiple events are returned ordered by seq."""
        trail = AuditTrail()
        e1 = _make_event(session_id="s1", seq=1)
        e3 = _make_event(session_id="s1", seq=3)
        e2 = _make_event(session_id="s1", seq=2)

        # Insert out of order
        await trail.handle_event(e1)
        await trail.handle_event(e3)
        await trail.handle_event(e2)

        events = trail.get_events("s1")
        assert len(events) == 3
        assert [e.seq for e in events] == [1, 2, 3]

    async def test_persist_events_for_different_sessions(self) -> None:
        """Events for different sessions are stored separately."""
        trail = AuditTrail()
        e1 = _make_event(session_id="s1", seq=1)
        e2 = _make_event(session_id="s2", seq=1)

        await trail.handle_event(e1)
        await trail.handle_event(e2)

        assert len(trail.get_events("s1")) == 1
        assert len(trail.get_events("s2")) == 1
        assert trail.get_events("s1")[0].session_id == "s1"
        assert trail.get_events("s2")[0].session_id == "s2"

    async def test_get_events_empty_session(self) -> None:
        """Getting events for a non-existent session returns empty list."""
        trail = AuditTrail()
        assert trail.get_events("nonexistent") == []

    async def test_uses_correlation_id_as_session_key(self) -> None:
        """correlation_id is used as the session key when present."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=1, correlation_id="corr-1")

        await trail.handle_event(event)

        # Stored under correlation_id, not session_id
        assert len(trail.get_events("corr-1")) == 1
        assert trail.get_events("s1") == []


class TestAuditTrailDuplicateRejection:
    """Tests for duplicate (session_id, seq) rejection (Req 19.6)."""

    async def test_reject_duplicate_idempotently(self) -> None:
        """Duplicate (session_id, seq) is silently skipped."""
        trail = AuditTrail()
        e1 = _make_event(session_id="s1", seq=1, event_id="id-1")
        e2 = _make_event(session_id="s1", seq=1, event_id="id-2")

        await trail.handle_event(e1)
        await trail.handle_event(e2)  # Duplicate seq — should be skipped

        events = trail.get_events("s1")
        assert len(events) == 1
        assert events[0].event_id == "id-1"  # First one kept

    async def test_duplicate_does_not_raise(self) -> None:
        """Processing a duplicate does not raise an exception."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=5)

        await trail.handle_event(event)
        # This should not raise
        await trail.handle_event(event)

    async def test_same_seq_different_sessions_not_duplicate(self) -> None:
        """Same seq in different sessions is not a duplicate."""
        trail = AuditTrail()
        e1 = _make_event(session_id="s1", seq=1)
        e2 = _make_event(session_id="s2", seq=1)

        await trail.handle_event(e1)
        await trail.handle_event(e2)

        assert len(trail.get_events("s1")) == 1
        assert len(trail.get_events("s2")) == 1


class TestAuditTrailWriteFailure:
    """Tests for write failure handling (Req 19.5)."""

    async def test_retain_event_on_write_failure(self) -> None:
        """On write failure, the event is retained for retry."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=1)

        # Simulate a write failure by patching _persist_event
        with patch.object(
            trail, "_persist_event", side_effect=RuntimeError("disk full")
        ):
            with pytest.raises(AuditWriteError):
                await trail.handle_event(event)

        retained = trail.get_retained_events()
        assert len(retained) == 1
        assert retained[0] is event

    async def test_surface_write_error(self) -> None:
        """On write failure, the error is surfaced."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=1)

        with patch.object(
            trail, "_persist_event", side_effect=RuntimeError("disk full")
        ):
            with pytest.raises(AuditWriteError):
                await trail.handle_event(event)

        errors = trail.get_write_errors()
        assert len(errors) == 1
        assert "disk full" in str(errors[0])
        assert errors[0].event is event

    async def test_write_failure_does_not_mark_as_persisted(self) -> None:
        """A failed write does not mark (session_id, seq) as persisted."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=1)

        with patch.object(
            trail, "_persist_event", side_effect=RuntimeError("fail")
        ):
            with pytest.raises(AuditWriteError):
                await trail.handle_event(event)

        # The key is not in persisted set, so a retry can succeed
        assert ("s1", 1) not in trail._persisted_keys

        # Now persist normally
        await trail.handle_event(event)
        assert len(trail.get_events("s1")) == 1


class TestDecisionRecordWrites:
    """Tests for DecisionRecord writes (Req 19.1, 19.2)."""

    def test_write_decision_record(self) -> None:
        """A DecisionRecord is written with all fields."""
        trail = AuditTrail()

        record = trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 2, "max_attempts": 3},
            decision="retry",
            alternatives=["escalate", "skip"],
            caused_by_event_id="evt-123",
            session_id="s1",
        )

        assert record.kind == DecisionKind.RETRY
        assert record.subject == "task-1"
        assert record.inputs == {"attempt": 2, "max_attempts": 3}
        assert record.decision == "retry"
        assert record.alternatives == ["escalate", "skip"]
        assert record.session_id == "s1"

    def test_deterministic_rationale_from_inputs(self) -> None:
        """Rationale is deterministically constructed from inputs (Req 19.2)."""
        trail = AuditTrail()

        # Call twice with same inputs
        r1 = trail.write_decision(
            kind=DecisionKind.ESCALATE,
            subject="task-2",
            inputs={"failures": 3, "threshold": 3},
            decision="escalate",
            session_id="s1",
        )
        r2 = trail.write_decision(
            kind=DecisionKind.ESCALATE,
            subject="task-2",
            inputs={"failures": 3, "threshold": 3},
            decision="escalate",
            session_id="s1",
        )

        # Identical inputs produce identical rationale
        assert r1.rationale == r2.rationale
        assert r1.rationale != ""

    def test_rationale_changes_with_different_inputs(self) -> None:
        """Different inputs produce different rationale."""
        trail = AuditTrail()

        r1 = trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 1},
            decision="retry",
            session_id="s1",
        )
        r2 = trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 2},
            decision="retry",
            session_id="s1",
        )

        assert r1.rationale != r2.rationale

    def test_rationale_is_sorted_by_input_keys(self) -> None:
        """Rationale determinism relies on sorted input keys."""
        trail = AuditTrail()

        # Same inputs, different insertion order in dict
        r1 = trail.write_decision(
            kind=DecisionKind.SKIP,
            subject="task-3",
            inputs={"z_key": "z", "a_key": "a"},
            decision="skip",
            session_id="s1",
        )
        r2 = trail.write_decision(
            kind=DecisionKind.SKIP,
            subject="task-3",
            inputs={"a_key": "a", "z_key": "z"},
            decision="skip",
            session_id="s1",
        )

        assert r1.rationale == r2.rationale

    def test_get_decisions_for_session(self) -> None:
        """get_decisions returns all decisions for a session."""
        trail = AuditTrail()

        trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={},
            decision="retry",
            session_id="s1",
        )
        trail.write_decision(
            kind=DecisionKind.SKIP,
            subject="task-2",
            inputs={},
            decision="skip",
            session_id="s1",
        )
        trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-3",
            inputs={},
            decision="retry",
            session_id="s2",
        )

        s1_decisions = trail.get_decisions("s1")
        assert len(s1_decisions) == 2
        assert s1_decisions[0].subject == "task-1"
        assert s1_decisions[1].subject == "task-2"

        s2_decisions = trail.get_decisions("s2")
        assert len(s2_decisions) == 1

    def test_get_decisions_empty_session(self) -> None:
        """get_decisions returns empty list for unknown session."""
        trail = AuditTrail()
        assert trail.get_decisions("nonexistent") == []

    def test_get_decision_by_subject(self) -> None:
        """get_decision_by_subject returns the most recent match."""
        trail = AuditTrail()

        trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 1},
            decision="retry",
            session_id="s1",
        )
        trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="task-1",
            inputs={"attempt": 2},
            decision="retry",
            session_id="s1",
        )

        record = trail.get_decision_by_subject("s1", "task-1")
        assert record is not None
        assert record.inputs == {"attempt": 2}  # Most recent

    def test_get_decision_by_subject_not_found(self) -> None:
        """get_decision_by_subject returns None when not found."""
        trail = AuditTrail()
        assert trail.get_decision_by_subject("s1", "unknown") is None

    def test_write_decision_with_empty_inputs(self) -> None:
        """DecisionRecord with empty inputs still has a rationale."""
        trail = AuditTrail()

        record = trail.write_decision(
            kind=DecisionKind.CAPABILITY_TRANSITION,
            subject="provider-x",
            inputs={},
            decision="deregistered",
            session_id="s1",
        )

        assert record.rationale != ""
        assert "none" in record.rationale  # Empty inputs represented


class TestAuditTrailBusIntegration:
    """Tests for EventBus subscription integration."""

    async def test_subscribe_to_bus(self) -> None:
        """AuditTrail subscribes to bus with pattern '*'."""
        trail = AuditTrail()
        bus = EventBus()

        trail.subscribe_to_bus(bus)

        # Publish an event through the bus
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="test",
            payload={"task_id": "t1"},
            event_id=str(uuid.uuid4()),
        )
        published = await bus.publish(event)

        # The audit trail should have received it
        events = trail.get_events("s1")
        assert len(events) == 1
        assert events[0].seq == published.seq

    async def test_receives_all_event_types(self) -> None:
        """AuditTrail receives events of all types (subscribes to '*')."""
        trail = AuditTrail()
        bus = EventBus()
        trail.subscribe_to_bus(bus)

        types_to_test = [
            EventType.TASK_START,
            EventType.TASK_DONE,
            EventType.MODEL_SELECTED,
            EventType.ERROR,
        ]

        for i, event_type in enumerate(types_to_test, start=1):
            event = Event.create(
                type=event_type,
                session_id="s1",
                source="test",
                payload={"i": i},
                event_id=str(uuid.uuid4()),
            )
            await bus.publish(event)

        events = trail.get_events("s1")
        assert len(events) == len(types_to_test)

    async def test_idempotent_with_bus_retry(self) -> None:
        """Audit trail handles duplicate delivery gracefully (bus at-least-once)."""
        trail = AuditTrail()

        # Simulate the bus delivering the same event twice
        event = _make_event(session_id="s1", seq=5, event_id="evt-5")

        await trail.handle_event(event)
        await trail.handle_event(event)  # Duplicate

        events = trail.get_events("s1")
        assert len(events) == 1


class TestAuditTrailClearSession:
    """Tests for session cleanup."""

    async def test_clear_session_removes_events(self) -> None:
        """clear_session removes all audit data for the session."""
        trail = AuditTrail()
        e1 = _make_event(session_id="s1", seq=1)
        e2 = _make_event(session_id="s1", seq=2)

        await trail.handle_event(e1)
        await trail.handle_event(e2)
        trail.write_decision(
            kind=DecisionKind.RETRY,
            subject="t1",
            inputs={},
            decision="retry",
            session_id="s1",
        )

        trail.clear_session("s1")

        assert trail.get_events("s1") == []
        assert trail.get_decisions("s1") == []

    async def test_clear_session_does_not_affect_other_sessions(self) -> None:
        """Clearing one session does not affect another."""
        trail = AuditTrail()
        e1 = _make_event(session_id="s1", seq=1)
        e2 = _make_event(session_id="s2", seq=1)

        await trail.handle_event(e1)
        await trail.handle_event(e2)

        trail.clear_session("s1")

        assert trail.get_events("s1") == []
        assert len(trail.get_events("s2")) == 1

    async def test_clear_allows_re_persist_same_keys(self) -> None:
        """After clearing, the same (session_id, seq) can be persisted again."""
        trail = AuditTrail()
        event = _make_event(session_id="s1", seq=1)

        await trail.handle_event(event)
        trail.clear_session("s1")

        # Should be able to persist again
        await trail.handle_event(event)
        assert len(trail.get_events("s1")) == 1
