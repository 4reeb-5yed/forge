"""Unit tests for the EventBus — publish, subscribe, replay, and idempotency."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.runtime.events.bus import EventBus, MAX_DELIVERY_ATTEMPTS
from app.runtime.events.models import Event, EventType


def _make_event(
    session_id: str = "session-1",
    event_type: EventType = EventType.TASK_START,
    source: str = "test",
    seq: int = 0,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> Event:
    """Helper to create a valid test event."""
    return Event.create(
        type=event_type,
        session_id=session_id,
        source=source,
        payload={"test": True},
        causation_id=causation_id,
        correlation_id=correlation_id or session_id,
        event_id=str(uuid.uuid4()),
        seq=seq,
    )


class TestPublishSeqAssignment:
    """Test that publish assigns monotonic seq values."""

    async def test_first_event_gets_seq_1(self) -> None:
        bus = EventBus()
        event = _make_event()
        result = await bus.publish(event)
        assert result.seq == 1

    async def test_sequential_events_get_incrementing_seq(self) -> None:
        bus = EventBus()
        e1 = await bus.publish(_make_event())
        e2 = await bus.publish(_make_event())
        e3 = await bus.publish(_make_event())
        assert e1.seq == 1
        assert e2.seq == 2
        assert e3.seq == 3

    async def test_different_sessions_have_independent_seq(self) -> None:
        bus = EventBus()
        e1 = await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        e2 = await bus.publish(_make_event(session_id="s2", correlation_id="s2"))
        e3 = await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        assert e1.seq == 1
        assert e2.seq == 1  # independent counter
        assert e3.seq == 2

    async def test_concurrent_publishes_assign_unique_seq(self) -> None:
        bus = EventBus()
        events = [_make_event() for _ in range(20)]
        results = await asyncio.gather(*[bus.publish(e) for e in events])
        seqs = [r.seq for r in results]
        assert sorted(seqs) == list(range(1, 21))
        assert len(set(seqs)) == 20  # all unique


class TestSubscribeDelivery:
    """Test subscriber registration and event delivery."""

    async def test_subscriber_receives_matching_events(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("task.*", handler, "sub-1")
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        await bus.publish(_make_event(event_type=EventType.TASK_DONE))
        assert len(received) == 2

    async def test_subscriber_does_not_receive_non_matching_events(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("model.*", handler, "sub-1")
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        assert len(received) == 0

    async def test_wildcard_subscriber_receives_all_events(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*", handler, "sub-all")
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        await bus.publish(_make_event(event_type=EventType.MODEL_SELECTED))
        assert len(received) == 2

    async def test_glob_pattern_matches_suffix(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*.done", handler, "sub-done")
        await bus.publish(_make_event(event_type=EventType.TASK_DONE))
        await bus.publish(_make_event(event_type=EventType.BUILD_DONE))
        await bus.publish(_make_event(event_type=EventType.TASK_START))
        assert len(received) == 2

    async def test_unsubscribe_removes_subscriber(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*", handler, "sub-1")
        await bus.publish(_make_event())
        assert len(received) == 1

        bus.unsubscribe("sub-1")
        await bus.publish(_make_event())
        assert len(received) == 1  # no new delivery


class TestDeliveryRetry:
    """Test at-least-once delivery with retry on failure."""

    async def test_retries_on_handler_failure(self) -> None:
        bus = EventBus()
        call_count = 0

        async def failing_handler(event: Event) -> None:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient failure")

        bus.subscribe("*", failing_handler, "flaky-sub")
        await bus.publish(_make_event())
        assert call_count == 3  # failed twice, succeeded on third

    async def test_gives_up_after_max_attempts(self) -> None:
        bus = EventBus()
        call_count = 0

        async def always_failing_handler(event: Event) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("permanent failure")

        bus.subscribe("*", always_failing_handler, "bad-sub")
        # Should not raise — failure is logged, not propagated
        await bus.publish(_make_event())
        assert call_count == MAX_DELIVERY_ATTEMPTS


class TestReplay:
    """Test event replay functionality."""

    async def test_replay_returns_all_events_for_session(self) -> None:
        bus = EventBus()
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))

        events = await bus.replay("s1")
        assert len(events) == 3
        assert [e.seq for e in events] == [1, 2, 3]

    async def test_replay_since_seq_filters_correctly(self) -> None:
        bus = EventBus()
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))

        events = await bus.replay("s1", since_seq=2)
        assert len(events) == 1
        assert events[0].seq == 3

    async def test_replay_empty_session_returns_empty_list(self) -> None:
        bus = EventBus()
        events = await bus.replay("nonexistent")
        assert events == []

    async def test_replay_does_not_return_other_sessions(self) -> None:
        bus = EventBus()
        await bus.publish(_make_event(session_id="s1", correlation_id="s1"))
        await bus.publish(_make_event(session_id="s2", correlation_id="s2"))

        events = await bus.replay("s1")
        assert len(events) == 1
        assert events[0].session_id == "s1"


class TestIdempotency:
    """Test idempotency enforcement on (session_id, seq) pairs."""

    async def test_duplicate_event_is_noop(self) -> None:
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        bus.subscribe("*", handler, "sub-1")

        # Publish first event — gets seq 1
        event = _make_event()
        result = await bus.publish(event)
        assert result.seq == 1
        assert len(received) == 1

        # Re-publish same event with seq=1 — should be a no-op
        duplicate = _make_event(seq=1)
        result2 = await bus.publish(duplicate)
        assert result2.seq == 1
        assert len(received) == 1  # no additional delivery

    async def test_duplicate_does_not_add_to_storage(self) -> None:
        bus = EventBus()

        event = _make_event()
        await bus.publish(event)

        # Attempt duplicate with explicit seq
        duplicate = _make_event(seq=1)
        await bus.publish(duplicate)

        events = await bus.replay("session-1")
        assert len(events) == 1  # only one stored


class TestValidation:
    """Test event validation rejects incomplete events."""

    async def test_missing_source_raises(self) -> None:
        bus = EventBus()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="",  # empty
            correlation_id="s1",
            event_id="eid-1",
        )
        with pytest.raises(ValueError, match="source"):
            await bus.publish(event)

    async def test_missing_event_id_raises(self) -> None:
        bus = EventBus()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="s1",
            source="test",
            correlation_id="s1",
            event_id="",  # empty
        )
        with pytest.raises(ValueError, match="event_id"):
            await bus.publish(event)

    async def test_missing_correlation_id_raises(self) -> None:
        bus = EventBus()
        event = Event.create(
            type=EventType.TASK_START,
            session_id="",
            source="test",
            correlation_id="",  # empty
            event_id="eid-1",
        )
        with pytest.raises(ValueError, match="correlation_id"):
            await bus.publish(event)

    async def test_missing_payload_raises(self) -> None:
        """Requirement 17.2: payload must be present (not None)."""
        bus = EventBus()
        # Manually construct an event with payload=None to bypass factory defaults
        event = Event(
            schema_version=1,
            seq=0,
            session_id="s1",
            type=EventType.TASK_START,
            timestamp=None,  # type: ignore[arg-type]
            source="test",
            payload=None,  # type: ignore[arg-type]
            causation_id=None,
            correlation_id="s1",
            event_id="eid-1",
        )
        with pytest.raises(ValueError, match="payload"):
            await bus.publish(event)


class TestDeliveryOrdering:
    """Test at-least-once delivery in ascending seq order per session (Req 17.6)."""

    async def test_events_delivered_in_seq_order(self) -> None:
        """Requirement 17.6: delivery is in ascending seq order per session."""
        bus = EventBus()
        delivered_seqs: list[int] = []

        async def handler(event: Event) -> None:
            delivered_seqs.append(event.seq)

        bus.subscribe("*", handler, "ordered-sub")

        # Publish multiple events for the same session
        for _ in range(10):
            await bus.publish(_make_event())

        assert delivered_seqs == list(range(1, 11))

    async def test_concurrent_publishes_deliver_in_seq_order(self) -> None:
        """Requirement 17.6: even concurrent publishes deliver in seq order."""
        bus = EventBus()
        delivered_seqs: list[int] = []

        async def handler(event: Event) -> None:
            delivered_seqs.append(event.seq)

        bus.subscribe("*", handler, "ordered-sub")

        # Concurrent publishes all for the same session
        events = [_make_event() for _ in range(10)]
        await asyncio.gather(*[bus.publish(e) for e in events])

        # All seqs were delivered
        assert sorted(delivered_seqs) == list(range(1, 11))


class TestIdempotentSubscriber:
    """Test that duplicate (session_id, seq) pairs produce no additional side effect (Req 17.8)."""

    async def test_duplicate_seq_produces_no_additional_delivery(self) -> None:
        """Requirement 17.8: processing a duplicate pair produces no observable side effect."""
        bus = EventBus()
        side_effects: list[str] = []

        async def handler(event: Event) -> None:
            side_effects.append(f"delivered-seq-{event.seq}")

        bus.subscribe("*", handler, "idempotent-sub")

        # First publish — should deliver
        await bus.publish(_make_event())
        assert side_effects == ["delivered-seq-1"]

        # Attempt duplicate with seq=1 — should NOT deliver again
        duplicate = _make_event(seq=1)
        await bus.publish(duplicate)
        assert side_effects == ["delivered-seq-1"]  # no new side effect

    async def test_multiple_duplicates_produce_no_side_effects(self) -> None:
        """Requirement 17.8: multiple duplicate submissions are all no-ops."""
        bus = EventBus()
        delivery_count = 0

        async def handler(event: Event) -> None:
            nonlocal delivery_count
            delivery_count += 1

        bus.subscribe("*", handler, "counter-sub")

        # Publish original
        await bus.publish(_make_event())
        assert delivery_count == 1

        # Submit the same seq multiple times
        for _ in range(5):
            await bus.publish(_make_event(seq=1))

        assert delivery_count == 1  # still just the one delivery


class TestCausationValidation:
    """Test causation_id validation — Requirement 17.4, 17.5."""

    async def test_root_event_with_no_causation_is_valid(self) -> None:
        """Requirement 17.5: root events have causation_id=None."""
        bus = EventBus()
        event = _make_event(causation_id=None)
        result = await bus.publish(event)
        assert result.seq == 1
        assert result.causation_id is None

    async def test_causation_referencing_prior_event_is_valid(self) -> None:
        """Requirement 17.4: causation_id references event with smaller seq."""
        bus = EventBus()
        # Publish root event first
        root = _make_event(causation_id=None)
        root_result = await bus.publish(root)
        assert root_result.seq == 1

        # Publish caused event referencing root's event_id
        caused = _make_event(causation_id=root_result.event_id)
        caused_result = await bus.publish(caused)
        assert caused_result.seq == 2
        assert caused_result.causation_id == root_result.event_id

    async def test_causation_referencing_nonexistent_event_raises(self) -> None:
        """Requirement 17.4: causation_id must reference existing event."""
        bus = EventBus()
        event = _make_event(causation_id="nonexistent-event-id")
        with pytest.raises(ValueError, match="causation_id"):
            await bus.publish(event)

    async def test_causation_referencing_event_in_different_session_raises(self) -> None:
        """Requirement 17.4: causation_id must reference event in same session."""
        bus = EventBus()
        # Publish event in session s1
        e1 = _make_event(session_id="s1", correlation_id="s1")
        e1_result = await bus.publish(e1)

        # Try to publish event in session s2 referencing s1's event
        e2 = _make_event(
            session_id="s2",
            correlation_id="s2",
            causation_id=e1_result.event_id,
        )
        with pytest.raises(ValueError, match="causation_id"):
            await bus.publish(e2)

    async def test_causation_chain_multiple_levels(self) -> None:
        """Requirement 17.4: multi-level causation chains are valid."""
        bus = EventBus()
        # Root event
        root = _make_event(causation_id=None)
        root_result = await bus.publish(root)

        # Second event caused by root
        e2 = _make_event(causation_id=root_result.event_id)
        e2_result = await bus.publish(e2)

        # Third event caused by second
        e3 = _make_event(causation_id=e2_result.event_id)
        e3_result = await bus.publish(e3)

        assert e3_result.seq == 3
        assert e3_result.causation_id == e2_result.event_id
